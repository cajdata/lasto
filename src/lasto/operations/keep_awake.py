"""Keeps Windows awake while a live capture runs (docs/architecture.md §4).

SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) asks Windows not to sleep while the calling
thread holds the request, and ES_CONTINUOUS alone clears it.
- The request ends when that thread exits, so only the main thread, which lives for the whole capture,
  may make it, and only that thread may clear it.
- It prevents idle sleep only. The display may still turn off, and closing the lid still does what the
  Windows power settings say. That stays your choice.
- A refusal is reported, and never stops the capture.

Outside the safety core, ctypes is used only here and in the test plugin's hardware firewall
(lasto.sim.pytest_plugin), each an approved exemption in the structural tests. This module binds
this one kernel32 function, and loads nothing else.
"""

from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable
from types import TracebackType

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _require_the_main_thread(action: str) -> None:
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError(
            f"only the main thread may {action} the keep-awake request: it lasts only as long as its thread"
        )


def _set_thread_execution_state() -> Callable[[int], int]:
    function = ctypes.WinDLL("kernel32").SetThreadExecutionState
    function.argtypes = [ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    return function


class KeepAwake:
    """Holds Windows awake from start() until stop(), or for a with block."""

    def __init__(self, set_state: Callable[[int], int] | None = None) -> None:
        """set_state stands in for SetThreadExecutionState in tests."""
        self._set_state = set_state
        self.held = False

    def start(self) -> bool:
        """Ask Windows not to sleep. Returns whether it agreed."""
        _require_the_main_thread("make")
        if self._set_state is None:
            self._set_state = _set_thread_execution_state()
        self.held = self._set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) != 0
        return self.held

    def stop(self) -> None:
        """Clear the request. Stopping twice does nothing."""
        if self.held and self._set_state is not None:
            _require_the_main_thread("clear")
            self._set_state(ES_CONTINUOUS)
        self.held = False

    def __enter__(self) -> KeepAwake:
        self.start()
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.stop()
