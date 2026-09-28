"""Checks that run on the built site. Any problem fails the build.

These enforce the brief mechanically: one H1, heading order, unique titles and
descriptions, canonical URLs, landmarks, no third-party requests, working internal links
and anchors, the page budget, font coverage, copy rules, JSON-LD, the sitemap, and
security.txt. They also catch copy that goes stale when a phase ships.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sitegen import fonts
from sitegen.data import Roadmap

BUDGET = 100_000
DESCRIPTION_MAX = 160

BANNED = [
    "supercharge", "unlock", "seamless", "revolutioniz", "revolutionar", "game-changer", "game changer",
    "powerful", "cutting-edge", "cutting edge", "effortless", "leverage", "utilize", "blazing", "next-gen",
    "world-class", "best-in-class", "synergy", "empower", "unleash", "thrilled", "super excited", "passionate",
    "robust",
]
NOT_X_Y = [
    # "It's not X, it's Y" and "This isn't about X. It's about Y."
    re.compile(r"\b(?:it|this|that)[’']?s not\b[^.;,:\n]{1,40}[,.;:] (?:it|this|that)[’']?s\b", re.I),
    re.compile(r"\b(?:it|this|that) (?:isn[’']?t|is not)\b[^.;,:\n]{1,40}[,.;:] (?:it|this|that)[’']?s\b", re.I),
    # "X, not Y": "the laptop's USB port, not the truck"
    re.compile(r"\w, not (?:a |an |the |just |only )?\w+", re.I),
]
ZERO_WIDTH = {"⁠", "​", "­"}
DASHES = re.compile("[–—]")

# Brand names in visible text must be covered by the trademark note (site.toml trademarks).
BRANDS = {"Toyota": "Toyota", "Lexus": "Lexus", "Subaru": "Subaru", "PEAK": "PEAK-System", "PCAN": "PCAN",
          "OBDLink": "OBDLink", "Launch Creader": "Launch", "Creader": "Creader"}

# Phrases that stop being true when a phase is done.
STALE = {"nothing runs at the truck": 2, "none recorded yet": 2, "hasn't been done yet": 2}


class _Scan(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lang = ""
        self.title = ""
        self.meta: dict[str, str] = {}
        self.canonical = ""
        self.h1 = 0
        self.headings: list[int] = []
        self.ids: set[str] = set()
        self.landmarks: set[str] = set()
        self.links: list[tuple[str, str, str]] = []  # (tag, attr, url)
        self.images: list[dict[str, str | None]] = []
        self.text: list[str] = []
        self.jsonld: list[str] = []
        self._stack: list[str] = []
        self._in_title = False
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang", "")
        if "id" in a:
            self.ids.add(a["id"])
        if tag in ("header", "nav", "main", "footer"):
            self.landmarks.add(tag)
        if tag == "title":
            self._in_title = True
        if tag == "meta" and a.get("name"):
            self.meta[a["name"]] = a.get("content", "")
        if tag == "link" and a.get("rel") == "canonical":
            self.canonical = a.get("href", "")
        if re.fullmatch(r"h[1-6]", tag):
            level = int(tag[1])
            self.headings.append(level)
            self.h1 += level == 1
        for attr in ("href", "src"):
            if attr in a:
                self.links.append((tag, attr, a[attr]))
        if tag == "img":
            self.images.append(dict(a))
        if tag == "script":
            self._skip += 1
            self._stack.append(a.get("type", ""))
        if tag == "style":
            self._skip += 1
            self._stack.append("style")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._skip:
            if self._stack and self._stack[-1] == "application/ld+json":
                self.jsonld.append(data)
            return
        self.text.append(data)


def _copy_problems(where: str, text: str) -> list[str]:
    out = []
    if DASHES.search(text):
        out.append(f"{where}: em or en dash in the copy")
    low = text.lower()
    for w in BANNED:
        if re.search(r"\b" + re.escape(w), low):
            out.append(f"{where}: banned word {w!r}")
    for pattern in NOT_X_Y:
        m = pattern.search(text)
        if m:
            out.append(f"{where}: 'not X, it's Y' construction: {m.group(0)!r}")
    return out


def run(dist: Path, pages: list, site: dict, roadmap: Roadmap, text_cmap: frozenset[int], mono_cmap: frozenset[int]) -> list[str]:
    problems: list[str] = []
    base = site["base_url"]
    scans: dict[str, _Scan] = {}
    titles: dict[str, str] = {}
    descriptions: dict[str, str] = {}
    css = (dist / "css" / "site.css").stat().st_size
    font_bytes = sum(p.stat().st_size for p in (dist / "fonts").glob("*.woff2"))

    for page in pages:
        path = dist / page.out
        html = path.read_text(encoding="utf-8")
        s = _Scan()
        s.feed(html)
        scans[page.url] = s
        where = page.out
        if s.lang != site["language"]:
            problems.append(f"{where}: <html lang> is {s.lang!r}")
        if s.h1 != 1:
            problems.append(f"{where}: {s.h1} <h1> elements")
        last = 0
        for level in s.headings:
            if last and level > last + 1:
                problems.append(f"{where}: heading jumps from h{last} to h{level}")
            last = level
        for lm in ("header", "nav", "main", "footer"):
            if lm not in s.landmarks:
                problems.append(f"{where}: no <{lm}>")
        if "main" not in s.ids:
            problems.append(f"{where}: the skip link target #main is missing")
        title = s.title.strip()
        if not title:
            problems.append(f"{where}: no <title>")
        elif title in titles:
            problems.append(f"{where}: same <title> as {titles[title]}")
        titles[title] = where
        desc = s.meta.get("description", "")
        if not desc:
            problems.append(f"{where}: no meta description")
        elif len(desc) > DESCRIPTION_MAX:
            problems.append(f"{where}: meta description is {len(desc)} characters (keep it under {DESCRIPTION_MAX})")
        elif desc in descriptions:
            problems.append(f"{where}: same meta description as {descriptions[desc]}")
        descriptions[desc] = where
        if page.indexable and s.canonical != base + page.url:
            problems.append(f"{where}: canonical is {s.canonical!r}, expected {base + page.url!r}")
        if not page.indexable and s.meta.get("robots") != "noindex":
            problems.append(f"{where}: not indexable but has no robots noindex")
        if page.meta.get("jsonld") and not s.jsonld:
            problems.append(f"{where}: front matter asks for JSON-LD, but the page has none")
        for raw in s.jsonld:
            try:
                data = json.loads(raw)
                types = {n.get("@type") for n in data.get("@graph", [])}
                for t in page.meta.get("jsonld", []):
                    if t not in types:
                        problems.append(f"{where}: JSON-LD is missing {t}")
            except (json.JSONDecodeError, AttributeError) as exc:
                problems.append(f"{where}: JSON-LD doesn't parse: {exc}")
        for img in s.images:
            if not img.get("width") or not img.get("height") or img.get("alt") is None:
                problems.append(f"{where}: <img> without width, height, and alt")

        for tag, attr, url in s.links:
            parts = urlsplit(url)
            if parts.scheme in ("http", "https") and not url.startswith(base):
                if tag != "a":  # links out are fine; loading anything from another server isn't
                    problems.append(f"{where}: third-party request <{tag} {attr}={url!r}>")
                continue
            if parts.scheme in ("mailto", "tel", "data"):
                continue
            local = url[len(base):] if url.startswith(base) else url
            lp = urlsplit(local)
            if lp.path == "" and lp.fragment:
                if lp.fragment not in s.ids:
                    problems.append(f"{where}: link to #{lp.fragment}, which isn't on the page")
                continue
            target = dist / unquote(lp.path).lstrip("/")
            if lp.path.endswith("/"):
                target = target / "index.html"
            if not target.exists():
                problems.append(f"{where}: broken link {url!r}")
            elif lp.fragment and target.suffix == ".html":
                t = _Scan()
                t.feed(target.read_text(encoding="utf-8"))
                if lp.fragment not in t.ids:
                    problems.append(f"{where}: link {url!r} points to a missing anchor")

        weight = path.stat().st_size + css + font_bytes
        if weight > BUDGET:
            problems.append(f"{where}: {weight:,} bytes with CSS and fonts, over the {BUDGET:,} byte budget")

        text = " ".join(s.text)
        problems += _copy_problems(where, text)
        missing = sorted({
            c for c in text
            if ord(c) not in text_cmap and ord(c) not in mono_cmap and not c.isspace() and c not in ZERO_WIDTH
        })
        if missing:
            problems.append(f"{where}: characters the web fonts don't have: {''.join(missing)!r}")
        mono_missing = sorted({c for c in fonts.mono_text(html) if ord(c) not in mono_cmap and not c.isspace() and c not in ZERO_WIDTH})
        if mono_missing:
            problems.append(f"{where}: monospace text uses characters the mono subset doesn't have: {''.join(mono_missing)!r}")
        for brand, mark in BRANDS.items():
            if re.search(r"\b" + re.escape(brand) + r"\b", text) and mark not in site["trademarks"]:
                problems.append(f"{where}: mentions {brand} but the trademark note doesn't cover {mark}")
        problems += _stale(where, text, roadmap)

        for m in page.mirrors:
            mp = dist / m
            if not mp.exists():
                problems.append(f"{m}: missing Markdown mirror")
            else:
                mirror = mp.read_text(encoding="utf-8")
                problems += _copy_problems(m, mirror)
                problems += _stale(m, mirror, roadmap)

    # Site-wide files
    sitemap = (dist / "sitemap.xml").read_text(encoding="utf-8")
    locs = set(re.findall(r"<loc>([^<]+)</loc>", sitemap))
    expected = {base + p.url for p in pages if p.indexable}
    if locs != expected:
        problems.append(f"sitemap.xml lists {sorted(locs ^ expected)} differently from the pages")
    robots = (dist / "robots.txt").read_text(encoding="utf-8")
    if f"Sitemap: {base}/sitemap.xml" not in robots:
        problems.append("robots.txt doesn't point to the sitemap")
    sec = (dist / ".well-known" / "security.txt").read_text(encoding="utf-8")
    m = re.search(r"^Expires: (\S+)$", sec, re.M)
    if not m or "Contact: " not in sec:
        problems.append("security.txt needs Contact and Expires")
    else:
        expires = dt.datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
        days = (expires - dt.datetime.now(dt.timezone.utc)).days
        if not 60 <= days <= 365:
            problems.append(f"security.txt expires in {days} days (keep it between 60 and 365)")
    llms = (dist / "llms.txt").read_text(encoding="utf-8")
    if not llms.startswith("# ") or "\n> " not in llms:
        problems.append("llms.txt needs an H1 and a blockquote summary")
    for name in ("llms.txt", "llms-full.txt", "robots.txt"):
        text = (dist / name).read_text(encoding="utf-8")
        problems += _copy_problems(name, text)
        problems += _stale(name, text, roadmap)
    return problems


def _stale(where: str, text: str, roadmap: Roadmap) -> list[str]:
    low = text.lower()
    return [
        f"{where}: says {phrase!r}, but Phase {phase} is done"
        for phrase, phase in STALE.items()
        if roadmap.phase(phase).done and phrase in low
    ]
