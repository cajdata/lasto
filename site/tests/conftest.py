import sys
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SITE))


@pytest.fixture(scope="session")
def built(tmp_path_factory):
    """One full build of the site into a temp folder, shared by the tests that need it."""
    from sitegen import checks
    from sitegen.pages import build

    out = tmp_path_factory.mktemp("dist")
    b = build(out)
    problems = checks.run(out, b.pages, b.site, b.roadmap, b.webfonts.text_cmap, b.webfonts.mono_cmap)
    return out, b, problems
