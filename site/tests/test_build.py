"""A full build, checked end to end."""

import datetime as dt
import json
import re

import pytest

from sitegen import figures, safety


def test_build_has_no_problems(built):
    _, _, problems = built
    assert problems == []


def test_site_files_exist(built):
    out, b, _ = built
    for rel in ["index.html", "index.md", "404.html", "robots.txt", "sitemap.xml", "llms.txt", "llms-full.txt",
                ".well-known/security.txt", "favicon.ico", "favicon.svg", "icon-192.png", "apple-touch-icon.png",
                "fonts/archivo.woff2", "fonts/fragment-mono.woff2", "fonts/OFL-Archivo.txt", "fonts/OFL-FragmentMono.txt",
                "og/github-social-preview.png", "css/site.css"]:
        assert (out / rel).exists(), rel
    for page in b.pages:
        for m in page.mirrors:
            assert (out / m).exists(), m


def test_service_map_is_generated_from_source(built):
    # Drawn from the frozen fixture (tests/fixtures/README.md): 14 allowed services and 22 never.
    import source_edit as se

    _, b, _ = built
    f = safety.load_facts(se.FIXTURE)
    services = {"allowed": {s: "a service" for s in f.allowed_services}, "never": {s: "a service" for s in f.never_services}}
    fig = figures.service_map({"facts": f, "services": services, "roadmap": b.roadmap}, 1)
    allow = re.search(r'<path class="allow" d="([^"]+)"', fig.html).group(1)
    never = re.search(r'<path class="never" d="([^"]+)"', fig.html).group(1)
    assert allow.count("z") == 14 and never.count("z") == 22
    # A square sits at its service's row (high nibble) and column (low nibble):
    # x = 28 + 2 + 18*column, y = 18 + 2 + 18*row. 0x22 is row 2, column 2, and 0x01 row 0, column 1.
    assert "M66 56h14v14h-14z" in allow and "M48 20h14v14h-14z" in allow
    assert "M102.5 20.5h13v13h-13z" in never  # 0x04, drawn as an outline on the half pixel


def test_sitemap_and_404(built):
    out, b, _ = built
    sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
    assert "404" not in sitemap
    assert "https://lasto.dev/safety/" in sitemap
    page = (out / "404.html").read_text(encoding="utf-8")
    assert '<meta name="robots" content="noindex">' in page
    assert 'href="/css/site.css"' in page  # root-absolute, so it works at any path


def test_security_txt(built):
    out, b, _ = built
    text = (out / ".well-known" / "security.txt").read_text(encoding="utf-8")
    assert "Contact: https://github.com/cajdata/lasto/security/advisories/new" in text
    expires = dt.datetime.fromisoformat(re.search(r"Expires: (\S+)", text).group(1).replace("Z", "+00:00"))
    days = (expires - dt.datetime.now(dt.timezone.utc)).days
    assert 300 <= days <= 331


def test_jsonld_types(built):
    out, _, _ = built
    home = (out / "index.html").read_text(encoding="utf-8")
    data = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', home, re.S).group(1))
    types = {n["@type"] for n in data["@graph"]}
    assert {"WebSite", "SoftwareApplication", "SoftwareSourceCode", "Person"} <= types
    app = next(n for n in data["@graph"] if n["@type"] == "SoftwareApplication")
    assert app["offers"]["price"] == "0" and app["operatingSystem"] == "Windows"
    assert "aggregateRating" not in app and "review" not in app  # never invented


def test_no_third_party_requests_and_strict_csp(built):
    out, b, _ = built
    for page in b.pages:
        html = (out / page.out).read_text(encoding="utf-8")
        assert "script-src 'none'" in html
        assert "<script src" not in html and "<style" not in html and ' style="' not in html
        for m in re.finditer(r'<(link|img|script|source)\b[^>]*\b(?:href|src)="(https?://[^"]+)"', html):
            assert m.group(2).startswith("https://lasto.dev"), m.group(0)


def test_trademark_note_drops_subaru(built):
    out, _, _ = built
    for f in out.rglob("*.html"):
        assert "Subaru" not in f.read_text(encoding="utf-8")


def test_unpushed_phase_gets_no_code_links(built):
    out, b, _ = built
    if not b.roadmap.phase(1).public:
        assert "/src/lasto/safety/" not in (out / "safety" / "index.html").read_text(encoding="utf-8")


def test_public_safety_core_gets_permalinks(built):
    out, b, _ = built
    if b.roadmap.phase(1).public:
        # 12 characters of the commit: GitHub resolves them, and they save page weight on every code link.
        rules = (out / "safety" / "index.html").read_text(encoding="utf-8")
        assert re.search(r'href="https://github\.com/cajdata/lasto/blob/[0-9a-f]{12}/src/lasto/safety/policy\.py#L\d+', rules)
        core = (out / "safety" / "core" / "index.html").read_text(encoding="utf-8")
        assert re.search(r'href="https://github\.com/cajdata/lasto/blob/[0-9a-f]{12}/src/lasto/safety/pcan_active\.py"', core)
        assert not re.search(r"/blob/[0-9a-f]{13,}/", rules + core)


def test_safety_pages_cover_the_safety_model(built):
    out, _, _ = built
    rules = (out / "safety" / "index.html").read_text(encoding="utf-8")
    for anchor in ("listen-only", "polled", "modules", "kill-switch", "enforced", "threat-model", "not-proven"):
        assert f'id="{anchor}"' in rules, anchor
    core = (out / "safety" / "core" / "index.html").read_text(encoding="utf-8")
    for anchor in ("on-the-wire", "writer", "frozen", "serial-guard", "audit", "tests"):
        assert f'id="{anchor}"' in core, anchor
        assert f'href="/safety/core/#{anchor}"' in rules, anchor
    assert 'href="/safety/#threat-model"' in core


def test_external_link_icon_is_one_shared_path(built):
    out, b, _ = built
    for page in b.pages:
        html = (out / page.out).read_text(encoding="utf-8")
        assert html.count('id="ext"') == 1, page.out
        assert "M2.5 7.5l5-5M3.5 2.5h4v4" not in html.replace('<path id="ext" d="M2.5 7.5l5-5M3.5 2.5h4v4"/>', ""), page.out


def test_timing_diagram_draws_each_level_once(built):
    out, _, _ = built
    html = (out / "safety" / "core" / "index.html").read_text(encoding="utf-8")
    waves = re.findall(r'<path class="wave[^"]*" d="([^"]+)"', html)
    assert len(waves) == 2
    for d in waves:
        # A horizontal run is one H segment: an H is never followed by another H.
        assert not re.search(r"H[\d.]+H", d), d
    assert waves[1] == "M164 90.5H1011"  # listen-only: the transmit line never leaves recessive


def test_safety_page_date_follows_the_whole_safety_core():
    from sitegen import paths
    from sitegen.pages import page_dependencies

    deps = set(page_dependencies({"body": "{{ facts.ceiling }}"}))
    assert set(paths.SAFETY.glob("*.py")) <= deps
    assert paths.SAFETY / "serial_guard.py" in deps and paths.SAFETY / "pcan_active.py" in deps


def test_refuses_to_build_over_a_folder_it_didnt_make(tmp_path):
    import pytest

    from sitegen.data import BuildError
    from sitegen.pages import build

    (tmp_path / "keep.txt").write_text("not a build", encoding="utf-8")
    with pytest.raises(BuildError, match="doesn't look like an earlier build"):
        build(tmp_path)
    assert (tmp_path / "keep.txt").exists()


def test_refuses_to_build_into_the_repo(tmp_path):
    import pytest

    from sitegen import paths
    from sitegen.data import BuildError
    from sitegen.pages import build

    for target in (paths.ROOT, paths.SITE):
        with pytest.raises(BuildError, match="refusing"):
            build(target)


def test_reserved_docs_path_cant_publish_before_its_phase(tmp_path, monkeypatch):
    import shutil

    import pytest

    from sitegen import paths
    from sitegen.data import BuildError
    from sitegen.pages import build

    from sitegen.data import load_roadmap

    # The first reserved path whose phase isn't done, so approving a phase never breaks this test.
    roadmap = load_roadmap()
    target = next((r for r in roadmap.reserved if r.phase is not None and not roadmap.phase(r.phase).done), None)
    if target is None:
        pytest.skip("every reserved docs path's phase is done")
    content = tmp_path / "content"
    shutil.copytree(paths.CONTENT, content)
    name = target.path.strip("/").split("/")[-1]
    (content / "docs" / f"{name}.md").write_text("---\nh1: A doc\ndescription: x\n---\n\n## A {#a}\n", encoding="utf-8")
    monkeypatch.setattr(paths, "CONTENT", content)
    with pytest.raises(BuildError, match=f"belongs to Phase {target.phase}"):
        build(tmp_path / "dist")


@pytest.mark.parametrize("bad, match", [("Run `lasto drive --turbo`.", "--turbo"), ("```\nlasto fly\n```", "lasto fly")])
def test_a_page_body_showing_a_command_cli_py_lacks_fails_the_build(tmp_path, monkeypatch, bad, match):
    import shutil

    from sitegen import paths
    from sitegen.data import BuildError
    from sitegen.pages import build

    # The real pipeline, on a page that has a lede, so the body and the lede are both checked.
    content = tmp_path / "content"
    shutil.copytree(paths.CONTENT, content)
    doc = content / "docs" / "passive-capture.md"
    doc.write_text(doc.read_text(encoding="utf-8") + f"\n{bad}\n", encoding="utf-8")
    monkeypatch.setattr(paths, "CONTENT", content)
    with pytest.raises(BuildError, match=match):
        build(tmp_path / "dist")


def test_a_reserved_docs_path_without_a_phase_publishes(tmp_path, monkeypatch):
    import shutil

    from sitegen import paths
    from sitegen.pages import build

    # Docs that belong to no phase, like an FAQ, are reserved without one and publish whenever they're written.
    content, data_dir = tmp_path / "content", tmp_path / "data"
    shutil.copytree(paths.CONTENT, content)
    shutil.copytree(paths.DATA, data_dir)
    roadmap = data_dir / "roadmap.toml"
    roadmap.write_text(roadmap.read_text(encoding="utf-8") + '\n[[reserved]]\npath = "/docs/faq/"\ntitle = "FAQ"\n',
                       encoding="utf-8")
    (content / "docs" / "faq.md").write_text("---\nh1: FAQ\ndescription: Questions.\nlede: Answers.\n---\n\n## One {#one}\n\nYes.\n",
                                             encoding="utf-8")
    monkeypatch.setattr(paths, "CONTENT", content)
    monkeypatch.setattr(paths, "DATA", data_dir)
    out = tmp_path / "dist"
    build(out)
    assert 'href="/docs/faq/"' in (out / "docs" / "index.html").read_text(encoding="utf-8")
    assert "/docs/faq/" not in (out / "404.html").read_text(encoding="utf-8")


def test_serve_watches_the_app_files_the_docs_read(tmp_path):
    from sitegen import appfacts, paths
    from sitegen.cli import watch_roots
    from sitegen.serve import _mtimes

    one = tmp_path / "one.py"
    one.write_text("x", encoding="utf-8")
    assert one in _mtimes([one])  # a single file, as well as folders
    roots = watch_roots()
    assert paths.SAFETY in roots and all(paths.APP / rel in roots for rel in appfacts.FILES)
    # And everything else a page is built or dated from: requires-python, and each page's sources:.
    assert paths.ROOT / "pyproject.toml" in roots
    assert paths.ROOT / "docs" / "architecture.md" in roots and paths.APP / "capture" / "recovery.py" in roots


def test_the_workflow_runs_when_a_page_s_sources_change():
    import fnmatch

    import yaml

    from sitegen import paths
    from sitegen.pages import FRONT_MATTER

    # A page's date follows its sources:, so a change to one must rebuild and redeploy the site.
    on = yaml.safe_load((paths.ROOT / ".github" / "workflows" / "site.yml").read_text(encoding="utf-8"))[True]
    for event in ("push", "pull_request"):
        patterns = on[event]["paths"]
        for page in paths.CONTENT.rglob("*.md"):
            meta = yaml.safe_load(FRONT_MATTER.match(page.read_text(encoding="utf-8").replace("\r\n", "\n")).group(1))
            for source in meta.get("sources", []):
                assert any(fnmatch.fnmatch(source, p) for p in patterns), (event, page.name, source)


def test_a_docs_page_needs_a_path_the_roadmap_reserves(tmp_path, monkeypatch):
    import shutil

    import pytest

    from sitegen import paths
    from sitegen.data import BuildError
    from sitegen.pages import build

    # Otherwise a doc for an unfinished phase could publish under a slightly different name.
    content = tmp_path / "content"
    shutil.copytree(paths.CONTENT, content)
    (content / "docs" / "polled.md").write_text("---\nh1: Polled\ndescription: x\n---\n\n## A {#a}\n", encoding="utf-8")
    monkeypatch.setattr(paths, "CONTENT", content)
    with pytest.raises(BuildError, match="reserve"):
        build(tmp_path / "dist")


def test_builds_are_repeatable(tmp_path):
    from sitegen.pages import build

    a, b = tmp_path / "a", tmp_path / "b"
    build(a)
    build(b)
    for f in a.rglob("*"):
        # Dates can differ while sources have uncommitted edits, so compare the binary and CSS outputs.
        if f.is_file() and f.suffix in (".woff2", ".png", ".css", ".ico"):
            assert f.read_bytes() == (b / f.relative_to(a)).read_bytes(), f.relative_to(a)
