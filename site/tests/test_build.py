"""A full build, checked end to end."""

import datetime as dt
import json
import re

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
    out, b, _ = built
    f = safety.load_facts()
    fig = figures.service_map({"facts": f, "services": b.services, "roadmap": b.roadmap}, 1)
    allow = re.search(r'<path class="allow" d="([^"]+)"', fig.html).group(1)
    never = re.search(r'<path class="never" d="([^"]+)"', fig.html).group(1)
    assert allow.count("z") == len(f.allowed_services)
    assert never.count("z") == len(f.never_services)
    # 0x22 is row 2, column 2: x = 28 + 2 + 18*2, y = 18 + 2 + 18*2
    assert "M66 56h14v14h-14z" in allow
    home = (out / "index.html").read_text(encoding="utf-8")
    assert f"<span>{len(f.allowed_services)} of 256</span>" in home


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

    content = tmp_path / "content"
    shutil.copytree(paths.CONTENT, content)
    (content / "docs").mkdir()
    (content / "docs" / "passive-capture.md").write_text("---\nh1: Passive capture\ndescription: x\n---\n\n## A {#a}\n", encoding="utf-8")
    monkeypatch.setattr(paths, "CONTENT", content)
    with pytest.raises(BuildError, match="belongs to Phase 2"):
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
