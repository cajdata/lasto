"""Records forbidden traffic the simulator receives, even if the code under test catches the error.

The pytest plugin (lasto.sim.pytest_plugin) fails any test that leaves a
violation behind. Tests that deliberately send forbidden traffic to check the
simulator itself wrap it in VIOLATIONS.expect().
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class ViolationRecorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[str] = []

    def record(self, message: str) -> None:
        with self._lock:
            self._items.append(message)

    def take(self) -> list[str]:
        with self._lock:
            items, self._items = self._items, []
        return items

    @contextmanager
    def expect(self) -> Iterator[list[str]]:
        """Collect the violations raised inside the block instead of failing the test."""
        saved = self.take()
        caught: list[str] = []
        try:
            yield caught
        finally:
            caught.extend(self.take())
            for item in saved:
                self.record(item)


VIOLATIONS = ViolationRecorder()
