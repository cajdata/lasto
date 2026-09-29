"""The link check over a built site, with the pinned lychee (sitegen/tools.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from sitegen import tools
from sitegen.data import BuildError


def check(dist: Path, *, online: bool = False) -> int:
    """lychee's exit code: 0 when every link and anchor resolves. Offline unless `online`, which adds external links.

    It runs inside `dist`, so no lychee.toml or [tool.lychee] from the repo can change what it checks.
    """
    dist = dist.resolve()
    if not (dist / "index.html").is_file():
        raise BuildError(f"{dist} has no built site to check; build it first")
    command = [str(tools.fetch("lychee")), "--no-progress", "--include-fragments", "--index-files", "index.html",
               "--root-dir", str(dist)]
    if not online:
        command.append("--offline")
    command.append("**/*.html")
    return subprocess.run(command, cwd=dist).returncode
