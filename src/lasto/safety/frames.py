"""What a CAN channel read can produce. Plain data; nothing here touches hardware."""

from __future__ import annotations

from dataclasses import dataclass

from lasto.safety._frozen import SealedType, freeze


@dataclass(frozen=True, slots=True)
class CanFrame(metaclass=SealedType):
    """A data (or remote) frame seen on the bus. timestamp_us is the interface's hardware clock."""

    can_id: int
    data: bytes
    timestamp_us: int
    extended: bool = False
    rtr: bool = False


@dataclass(frozen=True, slots=True)
class ErrorFrame(metaclass=SealedType):
    """A CAN error frame reported by the controller (error type, direction, position, counters)."""

    error_type: int
    data: bytes
    timestamp_us: int


@dataclass(frozen=True, slots=True)
class StatusMessage(metaclass=SealedType):
    """A driver status message: a change in bus state such as error-warning or bus-off."""

    status: int
    timestamp_us: int


@dataclass(frozen=True, slots=True)
class ReadError(metaclass=SealedType):
    """A read that failed with a non-message status, such as a receive overrun or a missing adapter."""

    status: int


Received = CanFrame | ErrorFrame | StatusMessage | ReadError


freeze(__name__)
