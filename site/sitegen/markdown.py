"""Markdown to datasheet HTML, and Markdown to the published .md mirror.

Content conventions (all plain CommonMark plus a few markers that read fine on GitHub):

- Every H2 and H3 ends with a fixed id: ``## Two modes {#modes}``. Ids never come from
  heading text, so rewording a heading can't break a link.
- ``Table: Quick reference data`` on the line before a table becomes its numbered caption.
- ``::: side`` ... ``:::`` puts blocks in the side column next to the section's text.
- Fenced ``figure NAME`` blocks are generated figures; fenced ``frames`` blocks are
  example CAN frames with a ``caption:`` first line.

Raw HTML in content is off (html=False), so everything outside the generated figures is
escaped. Smart quotes are on; the "replacements" rule stays off so -- never becomes a dash.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.container import container_plugin

from sitegen import figures
from sitegen.data import BuildError

HEADING_ID = re.compile(r'\s*\{#([a-z0-9][a-z0-9-]*)(?:\s+tag="([^"]+)")?\}\s*$')
# The word joiner keeps the arrow on the same line as the end of the link text. The arrow's
# path is drawn once per page, in the sprite in base.html, and each link points to it.
EXT_SVG = (
    "\u2060"  # word joiner
    '<svg class="ext" viewBox="0 0 10 10" width="10" height="10" aria-hidden="true">'
    '<use href="#ext"/></svg><span class="vh"> (external site)</span>'
)
NBSP = "\u00a0"

# Keep things that must be read as one token on one line: identifiers with hyphens that
# contain a capital or a digit (IPEH-002022, OBD-II, 2UZ-FE, GPL-3.0-or-later), dates,
# "Phase 4", and a number with its unit.
_NOBREAK_WORD = re.compile(r"(?<![\w-])(?=[\w.]*[A-Z0-9])[\w.]+(?:-[\w.]+)+(?![\w-])")
_NBSP_AFTER = re.compile(r"\b(Phase|Table|Fig\.|Ex\.|rev\.|Rev\.) (?=\d|[A-Z]\b)")
_NBSP_UNIT = re.compile(r"(\d) (?=(?:°F|°C|V|kbps|mph|rpm|ohms|seconds?|bytes|bits?|percent|per second)\b|°)")


def nobreak(escaped: str) -> str:
    """Apply no-break rules to text that is already HTML-escaped."""
    escaped = _NOBREAK_WORD.sub(lambda m: f'<span class="nw">{m.group(0)}</span>' if len(m.group(0)) <= 32 else m.group(0), escaped)
    escaped = _NBSP_AFTER.sub(lambda m: m.group(1) + NBSP, escaped)
    return _NBSP_UNIT.sub(lambda m: m.group(1) + NBSP, escaped)


@dataclass
class Heading:
    level: int
    id: str
    text: str
    number: str


class RenderEnv(dict):
    """Per-page render state. A dict, because markdown-it wants a mapping for its env."""

    def __init__(self, ctx: dict[str, Any], numbered: bool = True) -> None:
        super().__init__()
        self.ctx = ctx
        self.numbered = numbered
        self.headings: list[Heading] = []
        self.counters = {"h2": 0, "h3": 0, "fig": 0, "ex": 0, "table": 0}


# ---------------------------------------------------------------------------
# Core rules
# ---------------------------------------------------------------------------


def _heading_ids(state: Any) -> None:
    tokens = state.tokens
    for i, tok in enumerate(tokens):
        if tok.type != "heading_open":
            continue
        inline = tokens[i + 1]
        m = HEADING_ID.search(inline.content)
        if m:
            tok.attrSet("id", m.group(1))
            if m.group(2):
                tok.meta["tag"] = m.group(2)
            inline.content = inline.content[: m.start()]
            last = inline.children[-1] if inline.children else None
            if last is not None and last.type == "text":
                last.content = HEADING_ID.sub("", last.content)
        elif tok.tag in ("h2", "h3"):
            raise BuildError(f"heading {inline.content!r} needs a fixed id, like {{#some-id}}")


def _table_captions(state: Any) -> None:
    tokens: list[Token] = state.tokens
    i = 0
    while i + 3 < len(tokens):
        if (
            tokens[i].type == "paragraph_open"
            and tokens[i + 2].type == "paragraph_close"
            and tokens[i + 3].type == "table_open"
            and tokens[i + 1].content.startswith("Table: ")
        ):
            inline = tokens[i + 1]
            inline.content = inline.content[len("Table: ") :]
            first = inline.children[0] if inline.children else None
            if first is not None and first.type == "text" and first.content.startswith("Table: "):
                first.content = first.content[len("Table: ") :]
            tokens[i + 3].meta["caption"] = inline
            del tokens[i : i + 3]
            continue
        i += 1


def _table_headers(state: Any) -> None:
    """Column headers get scope=col; the first cell of each body row is a row header."""
    in_body = False
    first_in_row = False
    for tok in state.tokens:
        if tok.type == "tbody_open":
            in_body = True
        elif tok.type == "tbody_close":
            in_body = False
        elif tok.type == "tr_open":
            first_in_row = True
        elif tok.type == "th_open":
            tok.attrSet("scope", "col")
        elif tok.type in ("td_open", "td_close") and in_body and first_in_row:
            tok.tag = "th"
            if tok.type == "td_open":
                tok.attrSet("scope", "row")
            else:
                first_in_row = False


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _render_heading_open(self: Any, tokens: list[Token], idx: int, options: Any, env: RenderEnv) -> str:
    tok = tokens[idx]
    inline = tokens[idx + 1]
    text = "".join(c.content for c in (inline.children or []) if c.type in ("text", "code_inline")).strip() or inline.content.strip()
    hid = tok.attrGet("id") or ""
    number = ""
    if tok.tag in ("h2", "h3") and env.numbered:
        c = env.counters
        if tok.tag == "h2":
            c["h2"] += 1
            c["h3"] = 0
            number = str(c["h2"])
        else:
            c["h3"] += 1
            number = f"{c['h2']}.{c['h3']}"
    if tok.tag in ("h2", "h3"):
        env.headings.append(Heading(int(tok.tag[1]), hid, text, number))
    attrs = f' id="{html.escape(hid)}"' if hid else ""
    prefix = f'<span class="n">{number}</span> ' if number else ""
    return f"<{tok.tag}{attrs}>{prefix}"


def _render_heading_close(self: Any, tokens: list[Token], idx: int, options: Any, env: RenderEnv) -> str:
    tok = tokens[idx]
    opening = next(t for t in reversed(tokens[:idx]) if t.type == "heading_open")
    tag = opening.meta.get("tag")
    extra = f' <span class="tag">{nobreak(html.escape(tag))}</span>' if tag else ""
    return f"{extra}</{tok.tag}>" + "\n"


def _render_text(self: Any, tokens: list[Token], idx: int, options: Any, env: RenderEnv) -> str:
    return nobreak(html.escape(tokens[idx].content, quote=False))


def _is_external(href: str) -> bool:
    return href.startswith(("http://", "https://")) and not href.startswith("https://lasto.dev")


def _render_link_close(self: Any, tokens: list[Token], idx: int, options: Any, env: RenderEnv) -> str:
    # Find the matching link_open to see where it points.
    depth = 0
    for j in range(idx - 1, -1, -1):
        if tokens[j].type == "link_close":
            depth += 1
        elif tokens[j].type == "link_open":
            if depth == 0:
                if _is_external(str(tokens[j].attrGet("href") or "")):
                    return EXT_SVG + "</a>"
                break
            depth -= 1
    return "</a>"


def _render_table_open(self: Any, tokens: list[Token], idx: int, options: Any, env: RenderEnv) -> str:
    tok = tokens[idx]
    cls = ' class="qr"'
    cap = tok.meta.get("caption")
    if cap is None:
        return f"<table{cls}>"
    env.counters["table"] += 1
    inner = self.renderInline(cap.children, options, env)
    return f'<table{cls}><caption><b>Table {env.counters["table"]}.</b> {inner}</caption>'


def _render_fence(self: Any, tokens: list[Token], idx: int, options: Any, env: RenderEnv) -> str:
    tok = tokens[idx]
    kind, _, arg = tok.info.strip().partition(" ")
    if kind == "figure":
        return render_figure(arg.strip(), env).html
    if kind == "frames":
        env.counters["ex"] += 1
        md = env.ctx["md"]
        return figures.frames(tok.content, env.counters["ex"], lambda s: md.renderInline(s, env)).html
    lang = f' class="language-{html.escape(kind)}"' if kind else ""
    return f'<pre tabindex="0"><code{lang}>{html.escape(tok.content)}</code></pre>\n'


def render_figure(spec: str, env: RenderEnv) -> figures.Figure:
    name, _, arg = spec.partition(" ")
    func = figures.FIGURES.get(name)
    if func is None:
        raise BuildError(f"unknown figure {name!r}")
    env.counters["fig"] += 1
    return func(env.ctx, env.counters["fig"], arg) if arg else func(env.ctx, env.counters["fig"])


def make_parser() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "typographer": True})
    md.enable(["table", "smartquotes"])
    md.use(container_plugin, name="side", render=lambda *a, **k: "")
    md.core.ruler.after("inline", "lasto_heading_ids", _heading_ids)  # before smart quotes touch tag="..."
    md.core.ruler.push("lasto_table_captions", _table_captions)
    md.core.ruler.push("lasto_table_headers", _table_headers)
    md.add_render_rule("heading_open", _render_heading_open)
    md.add_render_rule("heading_close", _render_heading_close)
    md.add_render_rule("text", _render_text)
    md.add_render_rule("link_close", _render_link_close)
    md.add_render_rule("table_open", _render_table_open)
    md.add_render_rule("fence", _render_fence)
    return md


# ---------------------------------------------------------------------------
# Datasheet layout: sections, and main / side / wide columns
# ---------------------------------------------------------------------------


def _blocks(tokens: list[Token]) -> list[list[Token]]:
    out: list[list[Token]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.nesting == 1:
            depth = 0
            for j in range(i, len(tokens)):
                depth += tokens[j].nesting
                if depth == 0:
                    break
            out.append(tokens[i : j + 1])
            i = j + 1
        else:
            out.append([tok])
            i += 1
    return out


def _column(block: list[Token]) -> str:
    first = block[0]
    if first.type == "container_side_open":
        return "side"
    if first.type == "fence" and first.info.strip().startswith("figure "):
        return "wide"
    return "main"


def render_page(md: MarkdownIt, source: str, env: RenderEnv) -> str:
    """Render a page body into datasheet sections."""
    env.ctx["md"] = md
    tokens = md.parse(source, env)
    sections: list[tuple[str, list[tuple[str, str]]]] = [("", [])]
    for block in _blocks(tokens):
        if block[0].type == "heading_open" and block[0].tag == "h2":
            sections.append((block[0].attrGet("id") or "", []))
        col = _column(block)
        if col == "side" and any(t.type == "heading_open" for t in block):
            raise BuildError("a '::: side' block contains a heading; a closing ':::' is probably missing or glued to a line")
        if any(t.type == "inline" and t.content.rstrip().endswith(":::") for t in block):
            raise BuildError("a line ends in ':::'; the container marker needs a line of its own")
        body = block[1:-1] if col == "side" else block
        rendered = md.renderer.render(body, md.options, env)
        sections[-1][1].append((col, rendered))
    out = []
    for sid, parts in sections:
        if not parts:
            continue
        label = f' aria-labelledby="{html.escape(sid)}"' if sid else ""
        out.append(f'<section class="sec grid"{label}>')
        group_col, group = None, []
        for col, rendered in parts + [("end", "")]:
            if col == group_col and col != "wide":
                group.append(rendered)
                continue
            if group:
                if group_col == "wide":
                    out.append("".join(group))  # figures carry their own c-wide class
                else:
                    out.append(f'<div class="c-{group_col}">' + "".join(group) + "</div>")
            group_col, group = col, [rendered]
        out.append("</section>")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Mirrors: the same Markdown, cleaned up for people and agents reading .md files
# ---------------------------------------------------------------------------

LINK = re.compile(r"(\]\()(/[^)\s]*)")
FRAGMENT_LINK = re.compile(r"(\]\()(#[^)\s]*)")


def to_mirror(source: str, env: RenderEnv, base_url: str, page_url: str = "/") -> str:
    """Turn page source Markdown into the published mirror text."""
    out: list[str] = []
    lines = source.splitlines()
    i = 0
    fig_env = RenderEnv(env.ctx, numbered=False)
    tables = 0
    while i < len(lines):
        line = lines[i]
        fence = re.match(r"^(```+)\s*(\S*)\s*(.*)$", line)
        if fence:
            ticks, kind, arg = fence.groups()
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].startswith(ticks):
                body.append(lines[i])
                i += 1
            i += 1  # closing fence
            if kind == "figure":
                out.append(render_figure(arg.strip(), fig_env).markdown.rstrip())
            elif kind == "frames":
                fig_env.counters["ex"] += 1
                out.append(figures.frames("\n".join(body), fig_env.counters["ex"], lambda s: s).markdown.rstrip())
            else:
                out.append(f"{ticks}{kind}\n" + "\n".join(body) + f"\n{ticks}")
            continue
        if line.strip() in ("::: side", ":::"):
            i += 1
            continue
        if line.startswith("#"):
            line = HEADING_ID.sub("", line)
        if line.startswith("Table: "):
            tables += 1
            line = f"Table {tables}. {line[len('Table: '):]}"
        line = LINK.sub(lambda m: m.group(1) + base_url + m.group(2), line)
        line = FRAGMENT_LINK.sub(lambda m: m.group(1) + base_url + page_url + m.group(2), line)
        out.append(line)
        i += 1
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
