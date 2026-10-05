"""The docs section (W2): numbers read from the app's source, commands checked against cli.py,
stamps and summaries that follow the roadmap, and the docs index, breadcrumbs, and links."""

import copy
import datetime as dt
import json
import re

import pytest

from sitegen import appfacts, data, figures, paths, safety
from sitegen.data import BuildError, Phase, Roadmap
from sitegen.pages import (
    _stamp_gloss, app_summary, check_cli_mentions, llms_intro, page_dependencies, passive_sentence, truck_sentence,
)

D = dt.date(2026, 9, 26)


def _roadmap(*done: int, waiting: tuple[int, ...] = (), tested: tuple[int, ...] = ()) -> Roadmap:
    """Phases in `done` are approved, and Phases 2 and 4 among them were tested at the truck. `waiting`
    phases are built and waiting on approval, and `tested` ones have had their truck test."""
    phases = []
    for n in range(10):
        fields = dict(number=n, slug=f"p{n}", name=f"Phase {n}", status="planned", public=False, transmits="No",
                      summary="s", delivers=(), commands=(), live_test="", open=())
        if n in done:
            fields.update(status="done", built=D, approved=D, public=True)
        elif n in waiting:
            fields.update(status="awaiting-approval", built=D, public=True)
        if (n in (2, 4) and n in done) or n in tested:
            fields.update(live_tested=D)
        phases.append(Phase(**fields))
    return Roadmap("pre-alpha", phases)


def _copy_app(tmp_path):
    for rel in appfacts.FILES:
        dst = tmp_path / "src" / "lasto" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text((paths.APP / rel).read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def _edit(root, rel, old, new):
    p = root / "src" / "lasto" / rel
    text = p.read_text(encoding="utf-8")
    assert old in text, f"test setup: {old!r} not in {rel}"
    p.write_text(text.replace(old, new), encoding="utf-8")


def test_capture_facts_come_from_source():
    f = appfacts.load_capture_facts()
    assert f.silence == 60.0 and f.poll == 0.01 and f.tick == 1.0 and f.progress == 10.0
    assert f.flush_after == 1.5 and f.segment_seconds == 3600 and f.anchor_every == 60.0
    assert f.budget_gb == 20 and f.low_free_gb == 2 and f.newest_kept_days == 30
    assert f.simulated_seconds == 60.0
    assert f.poll_ms == 10 and f.segment_minutes == 60


def test_capture_facts_follow_a_change_in_the_source(tmp_path):
    root = _copy_app(tmp_path)
    _edit(root, "storage/retention.py", "BUDGET_BYTES = 20 * GIB", "BUDGET_BYTES = 25 * GIB")
    assert appfacts.load_capture_facts(root).budget_gb == 25


@pytest.mark.parametrize("rel, old, new, name", [
    ("operations/drive.py", "POLL = 0.01", "POLL = 0.0025", "POLL"),
    ("capture/recorder.py", "SEGMENT_SECONDS = 3600", "SEGMENT_SECONDS = 90", "SEGMENT_SECONDS"),
])
def test_capture_figures_the_docs_round_must_be_whole(tmp_path, rel, old, new, name):
    root = _copy_app(tmp_path)
    _edit(root, rel, old, new)
    with pytest.raises(BuildError, match=name):
        appfacts.load_capture_facts(root)


def test_the_budget_must_be_in_gib(tmp_path):
    root = _copy_app(tmp_path)
    _edit(root, "storage/retention.py", "GIB = 2**30", "GIB = 10**9")
    with pytest.raises(BuildError, match="GIB"):
        appfacts.load_capture_facts(root)


def test_phase_2_roadmap_numbers_match_the_source():
    # roadmap.toml isn't templated, so its numbers are checked here; the site tests run when the app changes.
    f, two = appfacts.load_capture_facts(), data.load_roadmap().phase(2)
    assert f"{f.silence:g} seconds without one ends it" in two.delivers[0]
    assert f"under a {f.budget_gb} GB budget" in two.open[0]
    assert f"under {f.low_free_gb} GB free" in two.open[0]


def test_a_page_naming_a_flag_or_command_cli_py_lacks_fails():
    facts = safety.load_facts()
    check_cli_mentions(
        "<p>Run <code>lasto drive --live --channel PCAN_USBBUS1</code>, then <code>lasto log last --bus</code>,"
        " or give <code>--seconds</code> or <code>--help</code>.</p>"
        "<pre><code>uv sync --locked\nlasto --version\nlasto log --help\n</code></pre>",
        facts, "page",
    )
    # lasto as a word in a path isn't the command, other tools' flags are left alone, and a command
    # continued on the next line keeps its own options.
    for fine in [
        "cd lasto &amp;&amp; uv run lasto drive --live",
        "PS C:\\src\\lasto&gt; lasto drive --live",
        ".venv\\Scripts\\lasto.exe drive --live",
        "git clone https://github.com/cajdata/lasto --depth 1",
        "lasto drive --live; lasto log --bus",
        "--- a/file\n+++ b/file",
        "uv run pytest \\\n  --no-cov",
        "lasto drive --live \\\n  --channel PCAN_USBBUS1",
        "uv run lasto \\\n  drive --live",  # broken before the command
        "lasto 0.0.1",  # what lasto --version prints
        "lasto 0.0.1: 'map' with the simulator arrives in Phase 3.",
        "https://github.com/cajdata/lasto --depth 1",
        "uv run --with lasto pytest",
        "lasto log --data E:\\lasto\\\nuv run pytest --no-cov",  # a folder ending in \ isn't a continuation
    ]:
        check_cli_mentions(f"<pre><code>{fine}\n</code></pre>", facts, "page")
    for html, match in [
        ("<pre><code>lasto log &amp;&amp; lasto fly\n</code></pre>", "lasto fly"),
        ("<pre><code>lasto drive --live; lasto log --live\n</code></pre>", "--live"),
        ("<pre><code>lasto drive --live \\\n  --bus\n</code></pre>", "--bus"),
        ("<pre><code>lasto drive --live `\n  --bus\n</code></pre>", "--bus"),
        ("<pre><code>lasto \\\n  fly\n</code></pre>", "lasto fly"),
        # Every way the page might show lasto being run is recognised, and so checked.
        ("<code>PS C:\\src&gt; lasto drive --turbo</code>", "--turbo"),
        ("<code>PS&gt; lasto drive --turbo</code>", "--turbo"),
        ("<code>C:\\src&gt;lasto fly</code>", "lasto fly"),
        ("<code>$ lasto fly</code>", "lasto fly"),
        ("<code>&gt; lasto fly</code>", "lasto fly"),
        ("<code>uv run lasto log --live</code>", "--live"),
        ("<code>uv run --locked lasto drive --turbo</code>", "--turbo"),
        ("<code>uvx lasto drive --bus</code>", "--bus"),
        ("<code>python -m lasto fly</code>", "lasto fly"),
        ("<code>python3 -m lasto fly</code>", "lasto fly"),
        ("<code>py -m lasto fly</code>", "lasto fly"),
        ("<code>.venv\\Scripts\\lasto.exe drive --turbo</code>", "--turbo"),
        ("<code>&amp; .\\lasto.exe drive --bus</code>", "--bus"),
        ("<code>&quot;C:\\Program Files\\lasto\\lasto.exe&quot; drive --turbo</code>", "--turbo"),
        ("<code>lasto.exe fly</code>", "lasto fly"),
        ("<code>cd x &amp;&amp; uv run lasto fly</code>", "lasto fly"),
        ("<code>lasto drive --turbo</code>", "--turbo"),
        ("<pre><code>lasto fly --live\n</code></pre>", "lasto fly"),
        ("<code>lasto log --live</code>", "--live"),  # a real option, but not one log takes
        ("<code>lasto drive --bus</code>", "--bus"),
        ("<code>lasto drive --Live</code>", "--Live"),
        ("<code>lasto drive2</code>", "drive2"),
        ("<code>lasto drive --live2</code>", "--live2"),
        ("<code>--turbo</code>", "--turbo"),
        ("<pre><code>lasto map --data x\n</code></pre>", "--data"),  # map doesn't take --data yet
    ]:
        with pytest.raises(BuildError, match=re.escape(match)):
            check_cli_mentions(html, facts, "page")


def test_preliminary_gloss_follows_the_truck():
    meta = {"phase": 1, "h1": "x"}
    assert _stamp_gloss("preliminary", meta, _roadmap(0, 1)).endswith("Nothing here has run at the truck.")
    assert _stamp_gloss("preliminary", meta, _roadmap(0, 1, 2)).endswith("At the truck, Lasto has only listened so far.")
    assert _stamp_gloss("preliminary", meta, _roadmap(0, 1, 2, 3, 4)).endswith(
        "Passive capture and polled reads have run at the truck.")


def test_the_truck_clause_follows_the_truck_tests_not_approval():
    # Each phase's truck test comes before its approval, so what has run at the truck can't wait for "done".
    polled = _roadmap(0, 1, 2, 3, waiting=(4,), tested=(4,))
    assert truck_sentence(polled) == "Passive capture and polled reads have run at the truck."
    assert "only listens" not in llms_intro(polled) and "only listens" not in polled.status_sentence()
    passive = _roadmap(0, 1, waiting=(2,), tested=(2,))
    assert truck_sentence(passive) == "At the truck, Lasto has only listened so far."
    assert "At the truck, Lasto only listens so far." in llms_intro(passive)
    assert passive.status_sentence().endswith("at the truck Lasto only listens so far.")
    assert truck_sentence(_roadmap(0, 1, waiting=(2,))) == "Nothing here has run at the truck."
    # Built and tested, waiting on approval: the rest of the summary agrees that it's built.
    assert "arriving in Phase 2" not in llms_intro(passive)
    assert "Passive capture, built in Phase 2 and run at the truck," in llms_intro(passive)
    assert "Passive mode (Phase 2) records" in app_summary(passive)


def test_summaries_follow_the_roadmap():
    assert "Passive mode (Phase 2) will record" in app_summary(_roadmap(0, 1))
    assert "Passive mode (Phase 2) records" in app_summary(_roadmap(0, 1, 2))
    assert "Polled mode (Phase 4) asks" in app_summary(_roadmap(0, 1, 2, 3, 4))
    assert passive_sentence(_roadmap(0, 1)).startswith("Passive mode, arriving in Phase 2,")
    assert passive_sentence(_roadmap(0, 1, 2)).startswith("Passive capture, built in Phase 2 and run at the truck,")


def test_llms_intro_follows_the_roadmap():
    before = llms_intro(_roadmap(0, 1))
    assert "Nothing runs at the truck until those phases ship." in before
    assert "Polled mode, arriving in Phase 4, will send" in before
    assert "At the truck, Lasto only listens so far." in llms_intro(_roadmap(0, 1, 2))
    after = llms_intro(_roadmap(0, 1, 2, 3, 4))
    assert "Polled mode, built in Phase 4, sends" in after
    assert "Nothing runs" not in after and "only listens" not in after


def test_page_dates_follow_only_the_data_a_page_uses():
    app = {paths.APP / rel for rel in appfacts.FILES}
    assert app <= set(page_dependencies({"body": "It runs for {{ capture.silence|num }} seconds."}))
    assert app <= set(page_dependencies({"body": "{% if capture.tick == 1 %}Once{% endif %}"}))
    # The same words in prose aren't a use: "in one capture." isn't capture data, and "this site." isn't site.toml.
    assert page_dependencies({"body": "At most 10 reopens in one capture. More on this site. See the roadmap. Facts."}) == []
    assert paths.DATA / "roadmap.toml" in page_dependencies({"body": "{% for p in roadmap.phases %}{% endfor %}"})
    docs = set(paths.CONTENT.glob("docs/*.md"))
    assert docs and docs <= set(page_dependencies({"body": "{% for d in docs %}{% endfor %}"}))
    # A Preliminary stamp's truck sentence and an app page's JSON-LD summary both come from the roadmap.
    assert paths.DATA / "roadmap.toml" in page_dependencies({"body": "Prose.", "stamp": "preliminary", "phase": 2})
    assert paths.DATA / "roadmap.toml" in page_dependencies({"body": "Prose.", "about_app": True})


def test_fig_2_needs_every_pin_it_labels():
    hw = copy.deepcopy(data.load_hardware())
    hw["port"]["pins"] = [p for p in hw["port"]["pins"] if p["signal"] != "SIL"]
    with pytest.raises(BuildError, match="SIL"):
        figures.pins_by_signal(hw)


def test_docs_index_and_breadcrumbs(built):
    out, b, _ = built
    index = (out / "docs" / "index.html").read_text(encoding="utf-8")
    assert 'href="/docs/passive-capture/"' in index
    crumbs = re.search(r'<nav class="crumbs"[^>]*>(.*?)</nav>', index, re.S).group(1)
    assert crumbs == '<ol><li><a href="/">Home</a></li><li><span aria-current="page">Docs</span></li></ol>'
    doc = (out / "docs" / "passive-capture" / "index.html").read_text(encoding="utf-8")
    crumbs = re.search(r'<nav class="crumbs"[^>]*>(.*?)</nav>', doc, re.S).group(1)
    assert crumbs == ('<ol><li><a href="/">Home</a></li><li><a href="/docs/">Docs</a></li>'
                      '<li><span aria-current="page">Passive capture</span></li></ol>')
    graph = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', doc, re.S).group(1))["@graph"]
    items = next(n for n in graph if n["@type"] == "BreadcrumbList")["itemListElement"]
    assert [c["item"] for c in items] == ["https://lasto.dev/", "https://lasto.dev/docs/", "https://lasto.dev/docs/passive-capture/"]
    assert [c["name"] for c in items] == ["Home", "Docs", "Passive capture"]
    assert [c["position"] for c in items] == [1, 2, 3]


def test_docs_is_in_the_nav(built):
    out, _, _ = built
    assert '<a href="/docs/"' in (out / "index.html").read_text(encoding="utf-8")


def test_404_lists_only_docs_still_to_come(built):
    out, b, _ = built
    page = (out / "404.html").read_text(encoding="utf-8")
    for r in b.roadmap.reserved:
        waiting = r.phase is not None and not b.roadmap.phase(r.phase).done  # a doc with no phase never waits
        assert (f"<code>{r.path}</code>" in page) == waiting, r.path


def test_roadmap_links_the_docs_a_done_phase_added(built):
    out, b, _ = built
    page = (out / "roadmap" / "index.html").read_text(encoding="utf-8")
    for r in b.roadmap.reserved:
        linked = r.phase is not None and b.roadmap.phase(r.phase).done  # a doc with no phase has no phase row
        assert (f'href="{r.path}"' in page) == linked, r.path


def test_chain_pin_labels_come_from_hardware_toml(built):
    template = (paths.TEMPLATES / "figures" / "chain.html").read_text(encoding="utf-8")
    assert not re.search(r'class="pin e"[^>]*>\d', template), "pin numbers are hard-coded in the template"
    out, b, _ = built
    home = (out / "index.html").read_text(encoding="utf-8")
    pins = {k: str(v["pin"]) for k, v in figures.pins_by_signal(b.hardware).items()}
    labels = lambda svg: re.findall(r'class="pin e"[^>]*>([^<]*)<', svg)  # noqa: E731
    wide = re.search(r'<svg class="f2 f2-wide".*?</svg>\s*<svg class="f2 f2-tall"', home, re.S).group(0)
    tall = re.search(r'<svg class="f2 f2-tall".*?<ol class="chain"', home, re.S).group(0)
    assert labels(wide) == [pins[s] for s in ("CANH", "CANL", "BAT", "SIL", "CG", "SG")]
    assert labels(tall) == [pins["CANH"], pins["CANL"], pins["SIL"], pins["BAT"], f"{pins['CG']} {pins['SG']}"]


def test_fig_2_keeps_identifiers_on_one_line(built):
    out, _, _ = built
    home = (out / "index.html").read_text(encoding="utf-8")
    caption = re.search(r'<figcaption id="fig\d+cap">(.*?)</figcaption>', home, re.S).group(1)
    assert '<span class="nw">PCAN-USB</span>' in caption
    assert '<span class="nw">K-line</span>' in re.search(r'<ol class="chain".*?</ol>', home, re.S).group(0)
