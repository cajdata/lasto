"""sitegen/tools.py on every path, whichever machine runs the tests.

Windows takes the zip assets and a warm cache, and Linux CI takes the tar.gz assets and an empty
cache, so each path is driven here directly: a tar.gz on Windows, a download into an empty cache,
and the executable bit Linux needs. A path that only ran on the other machine broke CI once.
"""

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from sitegen import paths, tools


def _archive(kind: str, member: str, content: bytes) -> bytes:
    buffer = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr(member, content)
    else:
        with tarfile.open(fileobj=buffer, mode="w:gz") as t:
            info = tarfile.TarInfo(member)
            info.size = len(content)
            t.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


@pytest.mark.parametrize("kind", ["zip", "tar.gz"])
def test_a_member_is_read_out_of_its_archive(kind):
    data = _archive(kind, "folder/tool", b"the binary")
    assert tools._member(data, f"release.{kind}", "folder/tool") == b"the binary"


@pytest.mark.parametrize(
    "name, key",
    [(name, key) for name, tool in tools.TOOLS.items() for key in tool.assets],
    ids=lambda v: v if isinstance(v, str) else "-".join(v),
)
def test_every_pinned_archive_holds_its_binary(name, key):
    # Every pin, not just this machine's: the real release archives, sha256 checked, for both systems.
    asset, data, member = tools.archive(name, key)
    binary = tools._member(data, asset, member)
    assert binary.startswith(b"MZ" if key[0] == "win32" else b"\x7fELF"), (asset, member)


@pytest.mark.parametrize("kind", ["zip", "tar.gz"])
def test_fetch_from_an_empty_cache_downloads_extracts_and_marks_executable(tmp_path, monkeypatch, kind):
    member, content = "tool-1.0/tool", b"\x7fELF a pretend binary"
    data = _archive(kind, member, content)
    fake = tools.Tool(version="1.0", url="https://example.invalid/{version}/{asset}",
                      assets={tools.platform_key(): (f"tool-{{version}}.{kind}", tools._sha256(data), member)})
    monkeypatch.setitem(tools.TOOLS, "fake", fake)
    monkeypatch.setattr(paths, "SITE", tmp_path)
    downloads: list[str] = []

    def urlopen(url, timeout):
        downloads.append(url)
        return io.BytesIO(data)

    monkeypatch.setattr(tools.urllib.request, "urlopen", urlopen)
    # Linux leaves a new file without its executable bit; Windows always says executable.
    monkeypatch.setattr(tools.os, "access", lambda path, mode: False)
    modes: list[int] = []
    monkeypatch.setattr(Path, "chmod", lambda self, mode: modes.append(mode))

    exe = tools.fetch("fake")
    assert exe == tmp_path / ".cache" / "fake" / "1.0" / "tool" and exe.read_bytes() == content
    assert downloads == [f"https://example.invalid/1.0/tool-1.0.{kind}"] and modes == [0o755]
    assert (tmp_path / ".cache" / "fake" / "1.0" / f"tool-1.0.{kind}").read_bytes() == data
    # The second time, the cached archive is checked and used, and nothing is downloaded again.
    assert tools.fetch("fake") == exe and len(downloads) == 1


def test_the_build_works_from_an_empty_cache(tmp_path, monkeypatch):
    # CI starts with no social-image fonts or rendered images cached; this machine rarely does.
    from sitegen.pages import build

    monkeypatch.setattr(paths, "CACHE", tmp_path / "cache")
    build(tmp_path / "dist")
    assert (tmp_path / "cache" / "og-fonts" / ".source-mtime").is_file()
    assert list((tmp_path / "cache" / "og").glob("*.png"))
    assert list((tmp_path / "dist").rglob("*.png"))
