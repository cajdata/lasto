"""Content conventions: fixed heading ids, captions, side column, mirrors, and escaping."""

import pytest

from sitegen import markdown
from sitegen.data import BuildError


@pytest.fixture
def md():
    return markdown.make_parser()


def render(md, text):
    env = markdown.RenderEnv({})
    return markdown.render_page(md, text, env), env


def test_headings_need_fixed_ids(md):
    with pytest.raises(BuildError, match="fixed id"):
        render(md, "## No id here\n")


def test_heading_ids_and_clause_numbers(md):
    html, env = render(md, "## First {#first}\n\ntext\n\n### Sub {#sub}\n\n## Second {#second}\n")
    assert '<h2 id="first"><span class="n">1</span> First</h2>' in html
    assert '<h3 id="sub"><span class="n">1.1</span> Sub</h3>' in html
    assert '<span class="n">2</span> Second' in html
    assert [h.id for h in env.headings] == ["first", "sub", "second"]


def test_table_caption_and_row_headers(md):
    html, _ = render(md, "## T {#t}\n\nTable: Pins\n\n| Pin | Use |\n|---|---|\n| 6 | CAN-H |\n")
    assert "<caption><b>Table 1.</b> Pins</caption>" in html
    assert '<th scope="col">Pin</th>' in html
    assert '<th scope="row">6</th>' in html


def test_side_column(md):
    html, _ = render(md, "## S {#s}\n\nmain text\n\n::: side\nside note\n:::\n")
    assert '<div class="c-main">' in html and '<div class="c-side"><p>side note</p>\n</div>' in html


def test_unclosed_side_block_fails(md):
    with pytest.raises(BuildError, match="contains a heading"):
        render(md, "## S {#s}\n\n::: side\nnote\n\n## Next {#next}\n")


def test_raw_html_is_escaped_and_dashes_are_left_alone(md):
    html, _ = render(md, '## H {#h}\n\n<script>alert(1)</script> and -- and "quotes"\n')
    assert "<script>" not in html
    assert "--" in html and "–" not in html and "—" not in html
    assert "“quotes”" in html


def test_external_links_get_a_marker(md):
    html, _ = render(md, "## H {#h}\n\n[out](https://github.com/x) and [in](/safety/)\n")
    assert 'out⁠<svg class="ext"' in html and '(external site)</span></a>' in html
    assert ">in</a>" in html
    assert html.count('class="ext"') == 1
    # The icon's path is drawn once per page, in the sprite, and each link points to it.
    assert '<use href="#ext"/>' in html and "<path" not in html


def test_mirror_is_clean_markdown():
    src = "## Title {#t}\n\nSee [safety](/safety/#x).\n\n::: side\nnote\n:::\n\nTable: Things\n"
    out = markdown.to_mirror(src, markdown.RenderEnv({}), "https://lasto.dev")
    assert "## Title\n" in out
    assert "{#" not in out and ":::" not in out
    assert "(https://lasto.dev/safety/#x)" in out
    assert "Table 1. Things" in out


def test_identifiers_dates_and_units_dont_break():
    out = markdown.nobreak("PEAK PCAN-USB (IPEH-002022), 500 kbps, Phase 4, 2026-09-26, 220 °F, read-only")
    assert '<span class="nw">IPEH-002022</span>' in out and '<span class="nw">2026-09-26</span>' in out
    assert "500 kbps" in out and "Phase 4" in out and "220 °F" in out
    assert "read-only" in out and '<span class="nw">read-only</span>' not in out


def test_heading_tag(md):
    html, _ = render(md, '## H {#h}\n\n### Polled mode {#polled tag="Phase 4"}\n')
    assert '<h3 id="polled"><span class="n">1.1</span> Polled mode <span class="tag">Phase 4</span></h3>' in html


def test_mirror_fragment_links_point_at_the_html_page():
    out = markdown.to_mirror("See [phase 2](#phase-2).\n", markdown.RenderEnv({}), "https://lasto.dev", "/roadmap/")
    assert "(https://lasto.dev/roadmap/#phase-2)" in out
