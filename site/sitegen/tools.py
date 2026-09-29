"""The release binaries the site's checks run: actionlint and lychee.

Each is its project's official release asset, pinned by version and by the sha256 the release
publishes. fetch() downloads it once into site/.cache, checks the archive's sha256 every time it's
called, and compares the binary on disk with the checked archive's copy every time, so only a
verified copy ever runs. Dependabot can't see these pins: to update a tool, change its version and copy the two
hashes from the new release.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import sys
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from sitegen import paths


class ToolError(Exception):
    """A pinned tool isn't available for this machine, or a download doesn't match its pin."""


@dataclass(frozen=True)
class Tool:
    version: str
    url: str  # with {version} and {asset}
    # (sys.platform, platform.machine()) -> (asset name with {version}, the asset's sha256, the binary inside it)
    assets: dict[tuple[str, str], tuple[str, str, str]]


TOOLS = {
    # sha256 from actionlint_1.7.12_checksums.txt on https://github.com/rhysd/actionlint/releases/tag/v1.7.12
    "actionlint": Tool(
        version="1.7.12",
        url="https://github.com/rhysd/actionlint/releases/download/v{version}/{asset}",
        assets={
            ("win32", "AMD64"): ("actionlint_{version}_windows_amd64.zip",
                                 "6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9", "actionlint.exe"),
            ("linux", "x86_64"): ("actionlint_{version}_linux_amd64.tar.gz",
                                  "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8", "actionlint"),
        },
    ),
    # sha256 from the .sha256 file beside each asset on https://github.com/lycheeverse/lychee/releases/tag/lychee-v0.24.2
    "lychee": Tool(
        version="0.24.2",
        url="https://github.com/lycheeverse/lychee/releases/download/lychee-v{version}/{asset}",
        assets={
            ("win32", "AMD64"): ("lychee-x86_64-pc-windows-msvc.zip",
                                 "32975d1493ee1a975d6bb41e4fb56fe419cb442ded628bb772ba2e614acfacad",
                                 "lychee-x86_64-pc-windows-msvc/lychee.exe"),
            ("linux", "x86_64"): ("lychee-x86_64-unknown-linux-gnu.tar.gz",
                                  "1f4e0ef7f6554a6ed33dd7ac144fb2e1bbed98598e7af973042fc5cd43951c9a",
                                  "lychee-x86_64-unknown-linux-gnu/lychee"),
        },
    ),
}


def platform_key() -> tuple[str, str]:
    return sys.platform, platform.machine()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def archive(name: str, key: tuple[str, str] | None = None) -> tuple[str, bytes, str]:
    """(asset name, its bytes, the binary inside it), from the cache or the release, sha256 checked either way."""
    tool = TOOLS[name]
    key = key or platform_key()
    if key not in tool.assets:
        raise ToolError(f"no pinned {name} for {key}; add it from the release's checksums")
    asset, sha256, member = tool.assets[key]
    asset = asset.format(version=tool.version)
    cached = paths.SITE / ".cache" / name / tool.version / asset
    data = cached.read_bytes() if cached.exists() else b""
    if _sha256(data) != sha256:
        url = tool.url.format(version=tool.version, asset=asset)
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                data = response.read()
        except OSError as exc:  # URLError and timeouts included
            raise ToolError(f"couldn't download {url}: {exc}") from None
        if _sha256(data) != sha256:
            raise ToolError(f"{url} doesn't match its pinned sha256")
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
    return asset, data, member


def _member(data: bytes, asset: str, member: str) -> bytes:
    """One named file out of a release archive."""
    try:
        if asset.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                return z.read(member)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            f = t.extractfile(member)
    except KeyError:
        f = None
    if f is None:  # missing, or not a regular file
        raise ToolError(f"{asset} has no file {member}")
    return f.read()


def fetch(name: str) -> Path:
    """The pinned tool for this machine, extracted from an archive whose sha256 was checked just now.

    The binary on disk is compared with the checked archive's copy every time and replaced if it
    differs, so only the checked bytes ever run. It isn't rewritten when it already matches, so a
    copy that's running (in another terminal, say) doesn't block the next one.
    """
    asset, data, member = archive(name)
    content = _member(data, asset, member)
    exe = paths.SITE / ".cache" / name / TOOLS[name].version / Path(member).name
    try:
        if not exe.is_file() or exe.read_bytes() != content:
            exe.write_bytes(content)
        if not os.access(exe, os.X_OK):
            exe.chmod(0o755)
    except OSError as exc:
        raise ToolError(f"couldn't put {name} at {exe}: {exc}") from None
    return exe
