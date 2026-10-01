"""Identifiers for runs, sessions, and vehicles: random UUIDs, so copies and merges never collide."""

from __future__ import annotations

import uuid


def new_id() -> str:
    return str(uuid.uuid4())
