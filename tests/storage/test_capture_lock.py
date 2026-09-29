"""The capture lock: one capture process at a time per data folder (lasto.storage.capture_lock)."""

from __future__ import annotations

import pytest

from lasto.storage.capture_lock import CaptureLock, CaptureRunning, capture_running
from lasto.storage.root import DataRoot


@pytest.fixture
def root(tmp_path) -> DataRoot:
    root = DataRoot(tmp_path)
    root.ensure()
    return root


def no_wait(seconds: float) -> None:
    pass


def test_one_capture_at_a_time(root):
    with CaptureLock.take(root, sleep=no_wait):
        assert capture_running(root)
        with pytest.raises(CaptureRunning, match="another capture"):
            CaptureLock.take(root, sleep=no_wait)
    assert not capture_running(root)
    CaptureLock.take(root, sleep=no_wait).release()


def test_asking_whether_a_capture_is_running_does_not_leave_the_lock_taken(root):
    assert not capture_running(root)
    assert not capture_running(root)
    CaptureLock.take(root, sleep=no_wait).release()


def test_asking_creates_nothing(root):
    assert not capture_running(root)
    assert not root.lock_file.exists()


def test_the_lock_is_released_when_the_capture_fails(root):
    with pytest.raises(RuntimeError), CaptureLock.take(root, sleep=no_wait):
        raise RuntimeError("the capture failed")
    assert not capture_running(root)


def test_a_capture_starting_while_the_lock_is_briefly_taken_waits_for_it(root):
    """Asking whether a capture is running takes the lock for a moment; a capture starting then still starts."""
    held = CaptureLock.take(root, sleep=no_wait)
    waits: list[float] = []

    def wait(seconds: float) -> None:
        waits.append(seconds)
        held.release()

    CaptureLock.take(root, sleep=wait).release()
    assert len(waits) == 1


def test_releasing_twice_does_nothing(root):
    lock = CaptureLock.take(root, sleep=no_wait)
    lock.release()
    lock.release()
    assert not capture_running(root)
