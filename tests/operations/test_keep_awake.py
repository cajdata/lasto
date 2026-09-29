"""Keeping Windows awake while a live capture runs (lasto.operations.keep_awake).

The tests hand KeepAwake a stand-in for SetThreadExecutionState; none of them asks Windows for anything.
"""

from __future__ import annotations

import threading

import pytest

from lasto.operations.keep_awake import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, KeepAwake


class Windows:
    """SetThreadExecutionState: returns the previous state, or 0 when it refuses."""

    def __init__(self, refuses: bool = False) -> None:
        self.calls: list[int] = []
        self.refuses = refuses

    def __call__(self, flags: int) -> int:
        self.calls.append(flags)
        return 0 if self.refuses else ES_CONTINUOUS


def test_asks_windows_not_to_sleep_until_it_stops():
    windows = Windows()
    with KeepAwake(windows) as awake:
        assert awake.held
        assert windows.calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED]
    assert windows.calls[1:] == [ES_CONTINUOUS]  # the request cleared
    assert not awake.held


def test_only_the_system_is_kept_awake_not_the_display():
    windows = Windows()
    with KeepAwake(windows):
        pass
    assert all(flags & 0x2 == 0 for flags in windows.calls)  # ES_DISPLAY_REQUIRED is never asked for


def test_a_refusal_is_reported_and_never_stops_the_capture():
    windows = Windows(refuses=True)
    with KeepAwake(windows) as awake:
        assert not awake.held
    assert windows.calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED]  # nothing to clear


def test_the_request_is_cleared_when_the_capture_fails():
    windows = Windows()
    with pytest.raises(RuntimeError), KeepAwake(windows):
        raise RuntimeError("the capture failed")
    assert windows.calls[-1] == ES_CONTINUOUS


def in_a_worker_thread(action) -> BaseException | None:
    """Run action on a short-lived thread, and return what it raised."""
    raised: list[BaseException] = []

    def run() -> None:
        try:
            action()
        except BaseException as exc:  # handed back to the test
            raised.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join()
    return raised[0] if raised else None


def test_only_the_main_thread_holds_the_request():
    """The request ends when the thread that made it exits, so a short-lived worker would drop it mid-capture."""
    windows = Windows()
    error = in_a_worker_thread(KeepAwake(windows).start)
    assert isinstance(error, RuntimeError) and "main thread" in str(error)
    assert windows.calls == []  # Windows was never asked


def test_the_request_is_cleared_on_the_thread_that_made_it():
    """Clearing it from another thread would clear nothing, and leave the capture's request standing."""
    windows = Windows()
    awake = KeepAwake(windows)
    awake.start()
    error = in_a_worker_thread(awake.stop)
    assert isinstance(error, RuntimeError) and "main thread" in str(error)
    assert awake.held and windows.calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED]
    awake.stop()
    assert windows.calls[-1] == ES_CONTINUOUS and not awake.held


def test_the_binding_is_one_kernel32_function():
    """Bound, never called: the only thing this module loads is kernel32's SetThreadExecutionState."""
    from lasto.operations import keep_awake

    function = keep_awake._set_thread_execution_state()
    assert function.__name__ == "SetThreadExecutionState"


def test_stopping_twice_clears_once():
    windows = Windows()
    awake = KeepAwake(windows)
    awake.start()
    awake.stop()
    awake.stop()
    assert windows.calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS]
