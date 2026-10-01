"""The per-package coverage gates the test command enforces (tests/coverage_gates.py).

The gate is fed real coverage data, recorded by `coverage run` in a subprocess over a tiny package,
so these tests never start a second tracer inside the suite's own measurement.
"""

from __future__ import annotations

import subprocess
import sys

import coverage
from coverage_gates import GATES, check

PACKAGE = {
    "lasto/__init__.py": "",
    "lasto/full/__init__.py": "",
    "lasto/full/a.py": "def f(x):\n    if x:\n        return 1\n    return 2\n",
    "lasto/partial/__init__.py": "",
    "lasto/partial/b.py": "def g(x):\n    if x:\n        return 1\n    return 2\n",
}
RUN = "from lasto.full.a import f\nfrom lasto.partial.b import g\nf(True)\nf(False)\ng(True)\n"


def measured(tmp_path) -> coverage.Coverage:
    for name, text in PACKAGE.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (tmp_path / "run.py").write_text(RUN, encoding="utf-8")
    data = tmp_path / ".coverage"
    subprocess.run(
        [sys.executable, "-m", "coverage", "run", "--branch", f"--data-file={data}", "--source=lasto", "run.py"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    cov = coverage.Coverage(data_file=str(data), branch=True)
    cov.load()
    return cov


def test_a_package_below_its_gate_fails(tmp_path):
    lines, failures = check(measured(tmp_path), {"lasto.full": 100.0, "lasto.partial": 90.0})
    assert failures == ["lasto.partial: 66.67% branch coverage, below its gate of 90%"]
    assert "lasto.full: 100.00% (gate 100%)" in lines


def test_a_package_at_or_above_its_gate_passes(tmp_path):
    _lines, failures = check(measured(tmp_path), {"lasto.full": 100.0, "lasto.partial": 60.0})
    assert failures == []


def test_a_package_with_no_code_yet_is_reported_not_failed(tmp_path):
    lines, failures = check(measured(tmp_path), {"lasto.storage": 90.0})
    assert failures == [] and lines == ["lasto.storage: no code measured yet (gate 90%)"]


def test_the_gates():
    """The safety core stays at 100%; capture and storage at 90%, crash-recovery and truncation paths included."""
    assert GATES == {"lasto.safety": 100.0, "lasto.capture": 90.0, "lasto.storage": 90.0}
