"""The link check: lychee, pinned by version and sha256 like actionlint, over the built site.

CI runs the same check with `python site/build.py links`. None of these skip.
"""

import io
import subprocess
import tarfile
import zipfile

import pytest

from sitegen import links, paths, tools


@pytest.fixture(scope="session")
def lychee():
    try:
        return tools.fetch("lychee")
    except tools.ToolError as exc:
        pytest.fail(str(exc))


def test_lychee_is_the_pinned_version(lychee):
    out = subprocess.run([str(lychee), "--version"], capture_output=True, encoding="utf-8", timeout=60, check=True).stdout
    assert out.split() == ["lychee", tools.TOOLS["lychee"].version]


def test_the_built_site_has_no_broken_links(built, lychee):
    out, _, _ = built
    assert links.check(out) == 0


def _site(root, broken: str | None) -> None:
    """A two-level site: the home page links to a nested page, which holds the link under test."""
    page = '<!doctype html><title>t</title><h1 id="top">t</h1>'
    (root / "index.html").write_text(page + '<a href="/a/b/">next</a>', encoding="utf-8")
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "index.html").write_text(page + '<a href="/#top">ok</a>' + (f'<a href="{broken}">x</a>' if broken else ""),
                                                 encoding="utf-8")


@pytest.mark.parametrize(("href", "named"), [("/missing/", "missing"), ("/#no-such-heading", "no-such-heading")])
def test_a_broken_link_or_anchor_on_a_nested_page_fails(tmp_path, lychee, capfd, href, named):
    _site(tmp_path, href)
    # lychee exits 2 for broken links, and also for a bad option, so the output must name the link it found.
    assert links.check(tmp_path) == 2
    out = "".join(capfd.readouterr())
    assert named in out and "Usage:" not in out


def test_the_same_site_without_the_broken_link_passes(tmp_path, lychee):
    _site(tmp_path, None)
    assert links.check(tmp_path) == 0


def test_a_download_that_doesnt_match_its_hash_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "SITE", tmp_path)
    monkeypatch.setattr(tools.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"not the release"))
    with pytest.raises(tools.ToolError, match="sha256"):
        tools.fetch("lychee")
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []  # nothing kept, nothing extracted


def test_a_stale_binary_beside_a_tampered_archive_is_never_used(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "SITE", tmp_path)
    tool = tools.TOOLS["lychee"]
    asset, _, member = tool.assets[tools.platform_key()]
    cached = tmp_path / ".cache" / "lychee" / tool.version / asset.format(version=tool.version)
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"tampered")
    stale = cached.parent / member.rsplit("/", 1)[-1]
    stale.write_bytes(b"a binary nothing has checked")
    monkeypatch.setattr(tools.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"still not the release"))
    with pytest.raises(tools.ToolError, match="sha256"):
        tools.fetch("lychee")
    assert stale.read_bytes() == b"a binary nothing has checked"  # refused, not run and not replaced


def test_fetch_works_while_the_tool_is_running(lychee):
    # Windows won't let a running .exe be overwritten, and Linux refuses writes to a running binary.
    running = subprocess.Popen([str(lychee), "--offline", "-"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        assert tools.fetch("lychee") == lychee
    finally:
        running.communicate(input=b"", timeout=60)


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_a_wrong_member_name_is_a_tool_error(kind):
    buffer = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr("folder/tool.exe", b"x")
    else:
        with tarfile.open(fileobj=buffer, mode="w:gz") as t:
            info = tarfile.TarInfo("folder/tool")
            info.size = 1
            t.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(tools.ToolError, match="has no file"):
        tools._member(buffer.getvalue(), f"release.{'zip' if kind == 'zip' else 'tar.gz'}", "tool")
