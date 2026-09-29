"""What a CAN channel produced, as plain data: the types storage, decoding, mapping, and the GUI use.

Hardware-free, and importing nothing from lasto, so every layer can use them without reaching the
safety core (docs/architecture.md §14.4). The capture code is the one place that converts from the
safety core's own frame types (lasto.capture.convert).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Frame:
    """A CAN frame, or an error frame, as the interface saw it. hw_us is the interface's hardware clock.

    For an error frame, can_id holds the controller's error type and data its detail bytes.
    """

    hw_us: int
    can_id: int
    data: bytes
    extended: bool = False
    rtr: bool = False
    error: bool = False


@dataclass(frozen=True, slots=True)
class BusEvent:
    """Something the interface reported that isn't a frame: a bus-state change, or a failed read."""

    kind: str  # "status" or "read_error"
    status: int  # the interface's status code
    hw_us: int | None = None  # when, if the interface timestamped it


Record = Frame | BusEvent
