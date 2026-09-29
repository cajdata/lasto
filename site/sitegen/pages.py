"""Load content, render pages, and write the site."""

from __future__ import annotations

import datetime as dt
import html as htmllib
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import jinja2
import yaml
from markupsafe import Markup

from sitegen import fonts, gitinfo, markdown, og, paths, safety, seo
from sitegen.data import BuildError, Roadmap, load_hardware, load_roadmap, load_services, load_site

FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)

STAMPS = {
    "objective": "Objective data sheet",
    "preliminary": "Preliminary",
    "status": "Development status",
    "info": "General information",
    "research": "Research notes",
}


@dataclass
class Page:
    source: Path
    meta: dict
    url: str
    out: str  # path under dist
    mirrors: list[str]
    title: str
    h1: str
    description: str
    lede: str
    stamp: str
    stamp_gloss: str
    indexable: bool
    crumbs: list[tuple[str, str]] = field(default_factory=list)
    modified: dt.datetime = field(default_factory=gitinfo.now)
    dirty: bool = False
    rev: str = "A"
    body_html: str = ""
    mirror_text: str = ""
    headings: list[markdown.Heading] = field(default_factory=list)
    og_path: str = ""
    canonical: str = ""
    og_image_url: str = ""

    @property
    def mirror_url(self) -> str:
        return "/" + self.mirrors[0] if self.mirrors else ""

    @property
    def slug(self) -> str:
        return self.url.strip("/").removesuffix(".html").replace("/", "-") or "home"

    @property
    def updated(self) -> str:
        return self.modified.date().isoformat()


def _url_for(rel: Path) -> tuple[str, str, list[str]]:
    """content path -> (url, html output path, mirror output paths)."""
    stem = rel.with_suffix("").as_posix()
    if stem == "404":
        return "/404.html", "404.html", []
    if stem == "index":
        return "/", "index.html", ["index.md"]
    return f"/{stem}/", f"{stem}/index.html", [f"{stem}/index.md", f"{stem}.md"]


def _stamp_gloss(kind: str, meta: dict, roadmap: Roadmap) -> str:
    if kind == "objective":
        return "What Lasto is being built to do."
    if kind == "preliminary":
        phase = roadmap.phase(int(meta["phase"]))
        if phase.status == "awaiting-approval":
            return "Built and tested against the simulator, and waiting on my sign-off. Nothing here has run at the truck."
        if phase.done:
            return "Built, approved, and tested against the simulator. Nothing here has run at the truck."
        raise BuildError(f"{meta['h1']!r}: a Preliminary stamp needs Phase {phase.number} to be built")
    if kind == "status":
        return "Generated from the roadmap file every time the site is built."
    if kind == "research":
        return "Nothing here is verified on the truck yet."
    return ""


def smart(text: str) -> str:
    """Curly quotes and apostrophes for front matter text, matching what the Markdown body gets."""
    text = re.sub(r'"([^"]*)"', "“\\1”", text)
    return text.replace("'", "’")


# Characters of a commit SHA in code permalinks. GitHub resolves an abbreviation, and at 12 it
# stays unique in a repository this size, while every link on a long page gets 28 bytes lighter.
PERMALINK_SHA = 12


def page_dependencies(meta: dict) -> list[Path]:
    """The data and source files a page's content is built from, found by what the page uses.

    A page's "Last updated" date, sitemap lastmod, and JSON-LD dateModified are the newest
    commit across its own source and these, so the dates move when (and only when) the
    content a reader sees can change. Template and CSS changes don't count.
    """
    body = meta["body"]
    deps: set[Path] = set()
    if "facts." in body or "code_link(" in body or "code_file(" in body or "figure service-map" in body:
        # The whole safety core: the facts come from several files, and every file link is a permalink.
        deps.update(paths.SAFETY.glob("*.py"))
        deps.add(paths.CLI)
    if "roadmap." in body or "status_sentence" in body or "figure service-map" in body or meta.get("layout") == "home":
        deps.add(paths.DATA / "roadmap.toml")
    if "hardware." in body or "figure chain" in body:
        deps.add(paths.DATA / "hardware.toml")
    if "services." in body or "figure service-map" in body:
        deps.add(paths.DATA / "services.toml")
    if "site." in body or meta.get("layout") == "home":
        deps.add(paths.DATA / "site.toml")
    if "python_req" in body:
        deps.add(paths.ROOT / "pyproject.toml")
    deps.update(paths.ROOT / extra for extra in meta.get("sources", []))
    return sorted(deps)


def python_requirement() -> str:
    """'Python 3.13 or later' from pyproject.toml's requires-python."""
    import tomllib

    spec = tomllib.loads((paths.ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["requires-python"]
    m = re.fullmatch(r">=\s*(\d+\.\d+)", spec.strip())
    if not m:
        raise BuildError(f"can't describe requires-python {spec!r} in words; update python_requirement()")
    return f"Python {m.group(1)} or later"


def minify_css(css: str) -> str:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"\s+", " ", css)
    css = re.sub(r"\s*([{};,>])\s*", r"\1", css)
    return css.replace(";}", "}").strip() + "\n"


class Builder:
    def __init__(self, out_dir: Path | None = None, strict: bool = False) -> None:
        self.out = out_dir or paths.DIST
        self.strict = strict
        self.site = load_site()
        self.roadmap = load_roadmap()
        self.services = load_services()
        self.hardware = load_hardware()
        self.facts = safety.load_facts()
        safety.check_services(self.facts, self.services)
        safety.check_commands(self.facts, self.roadmap)
        self.html_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(paths.TEMPLATES),
            autoescape=jinja2.select_autoescape(enabled_extensions=("html", "j2")),
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        self.content_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(paths.CONTENT),
            autoescape=False,
            undefined=jinja2.StrictUndefined,
            comment_start_string="{%#",  # {#id} marks heading ids in content, so Jinja comments use {%# #%}
            comment_end_string="#%}",
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        for env in (self.html_env, self.content_env):
            env.filters["hex2"] = lambda n: f"{n:02X}"
            env.filters["hex3"] = lambda n: f"{n:03X}"
            env.filters["num"] = lambda x: (f"{x:g}" if isinstance(x, float) else str(x))
        self.md = markdown.make_parser()
        self.ctx = {
            "site": self.site,
            "roadmap": self.roadmap,
            "facts": self.facts,
            "services": self.services,
            "hardware": self.hardware,
            "status_sentence": self.roadmap.status_sentence(),
            "roadmap_summary": "Lasto is a read-only data logger being built for a 2006 Lexus GX470, Lexus's version of the "
            "Toyota Land Cruiser Prado. Passive mode (Phase 2) will record what the truck's modules say on the CAN bus and send "
            "nothing. Polled mode (Phase 4) will ask read-only diagnostic questions for data the truck may not broadcast.",
            "python_req": python_requirement(),
            "render_partial": lambda name, **kw: self.html_env.get_template(name).render(**self.ctx, **kw),
            "code_link": self.code_link,
            "code_file": self.code_file,
        }
        self.pages: list[Page] = []

    # -- helpers used by content --------------------------------------------------------

    def code_link(self, name: str, label: str | None = None) -> str:
        """Markdown for a safety core constant: a permalink once Phase 1 is public, plain text before."""
        ref = self.facts.ref(name)
        text = f"`{label or ref.file.rsplit('/', 1)[-1]}`"
        if not self.roadmap.phase(1).public:
            return text
        sha = gitinfo.last_commit_sha(paths.ROOT / ref.file)
        if sha is None:
            return text
        lines = f"L{ref.line}" if ref.line == ref.end_line else f"L{ref.line}-L{ref.end_line}"
        return f"[{text}]({self.site['repo']}/blob/{sha[:PERMALINK_SHA]}/{ref.file}#{lines})"

    def code_file(self, name: str) -> str:
        """Markdown for a whole safety core file: a permalink once Phase 1 is public, plain text before."""
        path = paths.SAFETY / name
        if not path.exists():
            raise BuildError(f"content links to {name}, which isn't in the safety core")
        text = f"`{name}`"
        if not self.roadmap.phase(1).public:
            return text
        sha = gitinfo.last_commit_sha(path)
        if sha is None:
            return text
        return f"[{text}]({self.site['repo']}/blob/{sha[:PERMALINK_SHA]}/{path.relative_to(paths.ROOT).as_posix()})"

    # -- build -------------------------------------------------------------------------

    def load(self) -> None:
        for src in sorted(paths.CONTENT.rglob("*.md")):
            rel = src.relative_to(paths.CONTENT)
            raw = src.read_text(encoding="utf-8").replace("\r\n", "\n")
            m = FRONT_MATTER.match(raw)
            if not m:
                raise BuildError(f"{rel} has no front matter")
            meta = yaml.safe_load(m.group(1)) or {}
            meta["body"] = raw[m.end():]
            url, out, mirrors = _url_for(rel)
            stamp = meta.get("stamp", "")
            if stamp and stamp not in STAMPS:
                raise BuildError(f"{rel}: unknown stamp {stamp!r}")
            for key in ("h1", "description"):
                if not meta.get(key):
                    raise BuildError(f"{rel}: front matter needs {key}")
            page = Page(
                source=src,
                meta=meta,
                url=url,
                out=out,
                mirrors=mirrors,
                title=smart(meta.get("title") or f"{meta['h1']} | {self.site['name']}"),
                h1=smart(meta["h1"]),
                description=smart(meta["description"]),
                lede=meta.get("lede", ""),
                stamp=STAMPS.get(stamp, ""),
                stamp_gloss=_stamp_gloss(stamp, meta, self.roadmap) if stamp else "",
                indexable=not meta.get("noindex", False) and url != "/404.html",
            )
            page.canonical = self.site["base_url"] + url
            reserved = {r.path: r for r in self.roadmap.reserved}
            if url in reserved and not self.roadmap.phase(reserved[url].phase).done:
                raise BuildError(f"{rel}: {url} belongs to Phase {reserved[url].phase}, which isn't done; it can't be published yet")
            sources = [src] + page_dependencies(meta)
            for s in sources:
                if not s.exists():
                    raise BuildError(f"{rel}: source {s} doesn't exist")
            page.modified, page.dirty = gitinfo.last_modified(sources)
            page.rev = gitinfo.revision(src)
            self.pages.append(page)

    def render(self) -> None:
        for page in self.pages:
            ctx = dict(self.ctx, page=page)
            try:
                source = self.content_env.from_string(page.meta["body"]).render(**ctx)
            except jinja2.TemplateError as exc:
                raise BuildError(f"{page.source.name}: {exc}") from exc
            env = markdown.RenderEnv(ctx, numbered=page.meta.get("numbered", True))
            page.body_html = markdown.render_page(self.md, source, env)
            page.headings = env.headings
            page.meta["lede_html"] = Markup(self.md.renderInline(page.lede)) if page.lede else ""
            mirror_body = markdown.to_mirror(source, markdown.RenderEnv(ctx), self.site["base_url"], page.url)
            header = [f"# {page.h1}", ""]
            if page.lede:
                header += [page.lede, ""]
            facts = [f"- Page: {page.canonical}", f"- Last updated: {page.updated}", f"- By: {self.site['author']}"]
            if page.stamp:
                facts.insert(0, f"- Status: {page.stamp}. {page.stamp_gloss}")
            if page.url == "/":
                facts.insert(0, f"- Build status: {self.ctx['status_sentence']}")
            page.mirror_text = "\n".join(header + facts) + "\n\n" + mirror_body

    def write(self) -> list[Page]:
        out = self.out.resolve()
        protected = {paths.ROOT.resolve(), paths.SITE.resolve(), Path(out.anchor)}
        if out in protected or any(out in p.parents for p in (paths.ROOT.resolve() / "src", paths.CONTENT, paths.STATIC)):
            raise BuildError(f"refusing to build into {out}")
        if out.exists():
            previous_build = all((out / name).exists() for name in ("index.html", "sitemap.xml", "llms.txt", "robots.txt"))
            if any(out.iterdir()) and not previous_build:
                raise BuildError(f"{out} isn't empty and doesn't look like an earlier build; pick an empty or new folder")
            shutil.rmtree(out)
        out.mkdir(parents=True)
        built = gitinfo.now()

        # Static files first; fonts need the page text, so they come after rendering.
        for item in paths.STATIC.iterdir():
            if item.name == "fonts":
                continue
            if item.is_dir():
                shutil.copytree(item, self.out / item.name)
            else:
                shutil.copy2(item, self.out / item.name)
        css_path = self.out / "css" / "site.css"
        css_path.write_text(minify_css(css_path.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")
        og.write_icons(self.out)

        for page in self.pages:
            page.og_path = self._og_image(page)
            page.og_image_url = self.site["base_url"] + page.og_path
        self._social_preview()

        all_text = []
        mono = []
        for page in self.pages:
            ctx = dict(self.ctx, page=page, pages=self.pages)
            layout = page.meta.get("layout", "page")
            html_out = self.html_env.get_template(f"{layout}.html").render(**ctx, jsonld=seo.jsonld(page, self.ctx))
            target = self.out / page.out
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(html_out, encoding="utf-8", newline="\n")
            all_text.append(htmllib.unescape(re.sub(r"<[^>]+>", " ", html_out)))
            mono.append(fonts.mono_text(html_out))
            for m in page.mirrors:
                mp = self.out / m
                mp.parent.mkdir(parents=True, exist_ok=True)
                mp.write_text(page.mirror_text, encoding="utf-8", newline="\n")

        self.webfonts = fonts.build(self.out / "fonts", "".join(all_text), "".join(mono))

        base = self.site["base_url"]
        (self.out / "sitemap.xml").write_text(seo.sitemap(self.pages, base), encoding="utf-8", newline="\n")
        (self.out / "robots.txt").write_text(seo.robots(base), encoding="utf-8", newline="\n")
        (self.out / "llms.txt").write_text(seo.llms_txt(self.pages, self.ctx), encoding="utf-8", newline="\n")
        (self.out / "llms-full.txt").write_text(seo.llms_full(self.pages, self.ctx), encoding="utf-8", newline="\n")
        wk = self.out / ".well-known"
        wk.mkdir()
        (wk / "security.txt").write_text(seo.security_txt(self.site, built), encoding="utf-8", newline="\n")
        return self.pages

    def _og_image(self, page: Page) -> str:
        name = og.render_card(
            self.html_env, self.out / "og", page.slug, width=1200, height=630, stamp=page.stamp or "lasto.dev",
            title=page.h1, lede=page.meta.get("og_text", page.description), status=self.ctx["status_sentence"],
            allowed=self.facts.allowed_services, never=self.facts.never_services, polled_label=self._polled_label(),
        )
        return f"/og/{name}"

    def _social_preview(self) -> None:
        """The 1280x640 image for the GitHub repository's social preview setting (uploaded by hand)."""
        og.render_card(
            self.html_env, self.out / "og", "github-social-preview", width=1280, height=640,
            stamp="Objective data sheet", title="A read-only data logger for a 2006 Lexus GX470",
            lede="Passive mode sends nothing. Polled mode will ask read-only questions, and only through the allowlist.",
            # Uploaded to GitHub by hand, so it carries nothing that goes stale when a phase ships.
            status="Read-only by design. Free software under GPL-3.0-or-later.",
            allowed=self.facts.allowed_services, never=self.facts.never_services, polled_label=self._polled_label(),
            hashed=False,
        )

    def _polled_label(self) -> str:
        return "POLLED MODE" if self.roadmap.phase(4).done else "POLLED MODE, PHASE 4"


def build(out_dir: Path | None = None, strict: bool = False) -> Builder:
    if strict:
        if not gitinfo.available():
            raise BuildError("--strict needs git, for accurate dates")
        if gitinfo.is_shallow():
            raise BuildError("--strict needs full git history (actions/checkout with fetch-depth: 0)")
    b = Builder(out_dir, strict)
    b.load()
    b.render()
    b.write()
    if strict:
        dirty = [p.source.name for p in b.pages if p.dirty]
        if dirty:
            raise BuildError(f"--strict: uncommitted changes in the sources of {', '.join(dirty)}")
    return b
