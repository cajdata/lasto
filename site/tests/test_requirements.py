"""The site's requirements are exact and hash-pinned, and stay out of the app's metadata."""

import re
import tomllib

from sitegen import paths


def _blocks(name: str) -> list[str]:
    text = (paths.SITE / name).read_text(encoding="utf-8")
    return [b for b in re.split(r"\n(?=[A-Za-z])", text) if b.strip() and not b.startswith("#")]


def test_every_requirement_is_pinned_with_hashes():
    for f in ("requirements.txt", "requirements-test.txt"):
        for block in _blocks(f):
            first = block.splitlines()[0]
            assert re.match(r"^[A-Za-z0-9_.-]+==[^\s\\]+", first), f"{f}: {first}"
            assert "--hash=sha256:" in block, f"{f}: {first} has no hashes"


def test_site_dependencies_stay_out_of_the_app():
    project = tomllib.loads((paths.ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    deps = " ".join(project.get("dependencies", [])).lower()
    for name in ("jinja2", "markdown-it-py", "fonttools", "resvg"):
        assert name not in deps
