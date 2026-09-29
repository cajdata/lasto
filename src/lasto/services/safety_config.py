"""The safety core's configuration, read from a committed snapshot instead of from the core.

src/lasto/data/safety_config.json lists the allowlists, approved ECUs, rate limits, kill-switch and
interlock thresholds, and the STN command allowlist. Anything that shows them reads this file,
so it never imports the safety core: each run's record, and later the GUI's settings page
(docs/architecture.md §14.4). tests/test_safety_config.py proves the file matches the code
exactly.
"""

from __future__ import annotations

import json
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parent.parent / "data" / "safety_config.json"


def snapshot_text() -> str:
    """The snapshot exactly as committed: what a run records."""
    return SNAPSHOT.read_text(encoding="utf-8")


def snapshot() -> dict[str, object]:
    return json.loads(snapshot_text())
