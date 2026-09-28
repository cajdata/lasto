"""What a polled session hands back for each request: the Exchange and its state.

Kept apart from the gate so callers can use these types without importing the
module that transmits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from lasto.safety.requests import Request


class ExchangeState(Enum):
    PENDING = "pending"
    DONE = "done"
    NEGATIVE = "negative"
    TIMEOUT = "timeout"
    ABORTED = "aborted"


@dataclass
class Exchange:
    """One request and what came back for it."""

    request: Request
    can_id: int
    sent_at: float
    deadline: float
    state: ExchangeState = ExchangeState.PENDING
    responses: list[tuple[int, bytes]] = field(default_factory=list)
    nrc: int | None = None
    pending_extensions: int = 0
    rx_id: int | None = None
    rx_length: int = 0
    rx_data: bytearray = field(default_factory=bytearray)
    rx_sequence: int = 1
    flow_control_sent: bool = False

    @property
    def done(self) -> bool:
        return self.state is not ExchangeState.PENDING
