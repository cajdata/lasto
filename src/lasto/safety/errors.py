"""Exceptions raised by the safety core."""

from __future__ import annotations

from lasto.safety._frozen import SealedType, freeze


class SafetyError(Exception, metaclass=SealedType):
    """Base class for refusals raised on purpose by the safety core."""


class SafetyViolation(SafetyError):
    """A request, frame, or adapter command was refused by policy before reaching the interface."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class ImportRefused(SafetyViolation, ImportError):
    """An import the safety core refuses. Also an ImportError, so an optional import (try: import x / except
    ImportError) carries on without the module rather than failing (Step A review L13)."""


class KillSwitchTripped(SafetyError):
    """Transmission was attempted after the kill switch latched."""

    def __init__(self, cause: str) -> None:
        super().__init__(f"kill switch tripped: {cause}")
        self.reason = "kill_switch"
        self.cause = cause


class PassiveModeUnconfirmed(SafetyError):
    """Listen-only mode could not be confirmed, so passive capture refused to start."""


class InterfaceError(Exception, metaclass=SealedType):
    """The CAN interface or its driver reported an error, or couldn't be opened."""


class AdapterError(Exception, metaclass=SealedType):
    """The STN adapter gave a response the safety core can't accept."""


freeze(__name__)
