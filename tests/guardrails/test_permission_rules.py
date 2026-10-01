"""The permission rules in .claude/settings.json: what Claude Code denies, and what it asks before doing."""

from __future__ import annotations

import json
from pathlib import Path

SETTINGS = Path(__file__).resolve().parents[2] / ".claude" / "settings.json"


def rules(kind: str) -> set[str]:
    return set(json.loads(SETTINGS.read_text(encoding="utf-8"))["permissions"][kind])


def test_live_hardware_is_denied_in_the_shell():
    assert {"Bash(*--live*)", "PowerShell(*--live*)"} <= rules("deny")


def test_edits_that_need_the_owners_approval():
    """The safety core, the guardrails, and keep_awake.py, which uses ctypes outside the core (review finding L10)."""
    assert {
        "Edit(/src/lasto/safety/**)",
        "Edit(/src/lasto/operations/keep_awake.py)",
        "Edit(/.claude/**)",
        "Edit(/CLAUDE.md)",
    } <= rules("ask")
