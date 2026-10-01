"""The capture lock: one capture process at a time per data folder (docs/architecture.md §14.3).

- The capture process holds an OS lock on `capture.lock` in the data folder for its whole life.
  Windows releases it when the process ends, however it ends, so a crash never leaves a stale lock.
- Only the holder writes the capture database or recovers interrupted sessions.
- Anyone can ask whether a capture is running. Asking takes the lock for a moment, so a capture
  starting at that moment tries a few times before it gives up.
"""

from __future__ import annotations

import msvcrt
import os
import time
from collections.abc import Callable
from types import TracebackType

from lasto.storage.root import DataRoot

ATTEMPTS = 5
PAUSE = 0.05  # seconds between attempts


class CaptureRunning(Exception):
    """Another capture process holds the data folder's capture lock."""


def _lock(fd: int) -> bool:
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


class CaptureLock:
    """The capture lock, held. Release it, or use it as a context manager."""

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    @classmethod
    def take(cls, root: DataRoot, *, sleep: Callable[[float], None] = time.sleep) -> CaptureLock:
        fd = os.open(root.lock_file, os.O_RDWR | os.O_CREAT | os.O_BINARY, 0o644)
        for attempt in range(ATTEMPTS):
            if _lock(fd):
                return cls(fd)
            if attempt < ATTEMPTS - 1:
                sleep(PAUSE)
        os.close(fd)
        raise CaptureRunning(f"another capture is running on the data folder {root.path}")

    def release(self) -> None:
        """Release the lock. Releasing twice does nothing."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)

    def __enter__(self) -> CaptureLock:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.release()


def capture_running(root: DataRoot) -> bool:
    """Whether a capture process holds the data folder's capture lock. Creates nothing."""
    try:
        fd = os.open(root.lock_file, os.O_RDWR | os.O_BINARY)
    except FileNotFoundError:
        return False
    try:
        if not _lock(fd):
            return True
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return False
    finally:
        os.close(fd)
