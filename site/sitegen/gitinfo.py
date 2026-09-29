"""Dates and revisions from git, so "Last updated", sitemap lastmod, and JSON-LD agree.

A page's date is the newest commit touching its source or anything it's built from (a
data file, the safety core source). Uncommitted changes count as "now", so a local
preview shows the date the page would get. The revision letter counts the commits that
touched the page's own source file: rev. A is its first commit.
"""

from __future__ import annotations

import datetime as dt
import functools
import shutil
import subprocess
from pathlib import Path

from sitegen import paths
from sitegen.data import BuildError


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=paths.ROOT, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if result.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def available() -> bool:
    if shutil.which("git") is None:
        return False
    try:
        return _git("rev-parse", "--is-inside-work-tree") == "true"
    except BuildError:
        return False


def is_shallow() -> bool:
    return _git("rev-parse", "--is-shallow-repository") == "true"


def head() -> str:
    return _git("rev-parse", "HEAD")


def _rel(path: Path) -> str | None:
    """Repo-relative path, or None for a file outside the repository (treated as uncommitted)."""
    try:
        return path.resolve().relative_to(paths.ROOT.resolve()).as_posix()
    except ValueError:
        return None


@functools.cache
def _last_commit(rel: str) -> dt.datetime | None:
    out = _git("log", "-1", "--format=%cI", "--", rel)
    return dt.datetime.fromisoformat(out) if out else None


@functools.cache
def _dirty(rel: str) -> bool:
    return bool(_git("status", "--porcelain", "--", rel))


@functools.cache
def _commit_count(rel: str) -> int:
    return int(_git("rev-list", "--count", "HEAD", "--", rel) or 0)


def clear_caches() -> None:
    """Forget cached git answers, so a rebuild in the same process sees new commits and edits."""
    for fn in (_last_commit, _dirty, _commit_count):
        fn.cache_clear()


def now() -> dt.datetime:
    return dt.datetime.now().astimezone().replace(microsecond=0)


def last_modified(sources: list[Path]) -> tuple[dt.datetime, bool]:
    """Newest commit over sources, or now if any of them has uncommitted changes."""
    if not available():
        return now(), True
    dates: list[dt.datetime] = []
    dirty = False
    for src in sources:
        rel = _rel(src)
        if rel is None:
            dirty = True
            continue
        if _dirty(rel):
            dirty = True
        date = _last_commit(rel)
        if date is None:
            dirty = True
        else:
            dates.append(date)
    if dirty or not dates:
        return now(), True
    return max(dates), False


def revision(source: Path) -> str:
    """A, B, C ... Z, AA ... from the number of commits that touched the file."""
    if not available():
        return "A"
    rel = _rel(source)
    if rel is None:
        return "A"
    count = _commit_count(rel) + (1 if _dirty(rel) or _last_commit(rel) is None else 0)
    count = max(count, 1)
    letters = ""
    while count:
        count, rem = divmod(count - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def last_commit_sha(source: Path) -> str | None:
    """The newest commit touching a file, for permalinks to code."""
    if not available():
        return None
    rel = _rel(source)
    if rel is None:
        return None
    out = _git("log", "-1", "--format=%H", "--", rel)
    return out or None
