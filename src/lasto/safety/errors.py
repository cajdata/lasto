"""Exceptions raised by the safety core."""

from __future__ import annotations


class SafetyError(Exception):
    """Base class for refusals raised on purpose by the safety core."""


class SafetyViolation(SafetyError):
    """A request, frame, or adapter command was refused by policy before reaching the interface."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class KillSwitchTripped(SafetyError):
    """Transmission was attempted after the kill switch latched."""

    def __init__(self, cause: str) -> None:
        super().__init__(f"kill switch tripped: {cause}")
        self.reason = "kill_switch"
        self.cause = cause


class PassiveModeUnconfirmed(SafetyError):
    """Listen-only mode could not be confirmed, so passive capture refused to start."""


class InterfaceError(Exception):
    """The CAN interface or its driver reported an error, or couldn't be opened."""


class AdapterError(Exception):
    """The STN adapter gave a response the safety core can't accept."""
