"""Per-package branch coverage gates for the test command (docs/architecture.md §7).

pytest-cov measures all of lasto and prints its report. Then tests/conftest.py runs check() on the
same data and fails the run if a gated package is below its gate:
- the safety core stays at 100%;
- capture and storage, the code that must never lose a drive, are held at 90%, their truncation
  and crash-recovery paths included.
Everything else, CLI rendering among it, is reported only.
"""

from __future__ import annotations

import io

import coverage
from coverage.exceptions import NoDataError
from coverage.results import display_covered

GATES = {"lasto.safety": 100.0, "lasto.capture": 90.0, "lasto.storage": 90.0}


def check(cov: coverage.Coverage, gates: dict[str, float]) -> tuple[list[str], list[str]]:
    """(one line per gate, one message per gate that fails). A package with no code yet doesn't fail."""
    lines: list[str] = []
    failures: list[str] = []
    for package, gate in gates.items():
        pattern = "*/" + package.replace(".", "/") + "/*"
        try:
            percent = cov.report(include=[pattern], file=io.StringIO())
        except NoDataError:
            lines.append(f"{package}: no code measured yet (gate {gate:g}%)")
            continue
        shown = display_covered(percent, 2)  # never rounds up to 100 unless every branch is covered
        lines.append(f"{package}: {shown}% (gate {gate:g}%)")
        if percent < gate:
            failures.append(f"{package}: {shown}% branch coverage, below its gate of {gate:g}%")
    return lines, failures
