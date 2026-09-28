"""What a polled session hands back for each request: the Exchange and its state.

Kept apart from the gate so callers can use these types without importing the
module that transmits. An Exchange is read-only outside the safety core: the
gate keeps its progress (including the flow-control bookkeeping) in private
slots and is the only code that updates them.
"""

from __future__ import annotations

from lasto.safety._frozen import SealedEnum, SealedType, freeze
from lasto.safety.requests import Request


class ExchangeState(SealedEnum):
    PENDING = "pending"
    DONE = "done"
    NEGATIVE = "negative"
    TIMEOUT = "timeout"
    ABORTED = "aborted"


class Exchange(metaclass=SealedType):
    """One request and what came back for it."""

    __slots__ = (
        "_can_id", "_deadline", "_flow_control_sent", "_nrc", "_pending_extensions", "_request", "_responses",
        "_rx_data", "_rx_id", "_rx_length", "_rx_sequence", "_sent_at", "_state",
    )  # fmt: skip

    def __init__(self, request: Request, can_id: int, *, sent_at: float, deadline: float) -> None:
        self._request = request
        self._can_id = can_id
        self._sent_at = sent_at
        self._deadline = deadline
        self._state = ExchangeState.PENDING
        self._responses: list[tuple[int, bytes]] = []
        self._nrc: int | None = None
        self._pending_extensions = 0
        # A multi-frame answer being received: from whom, how long, what so far, the next sequence number.
        self._rx_id: int | None = None
        self._rx_length = 0
        self._rx_data = bytearray()
        self._rx_sequence = 1
        self._flow_control_sent = False

    @property
    def request(self) -> Request:
        return self._request

    @property
    def can_id(self) -> int:
        return self._can_id

    @property
    def sent_at(self) -> float:
        return self._sent_at

    @property
    def deadline(self) -> float:
        return self._deadline

    @property
    def state(self) -> ExchangeState:
        return self._state

    @property
    def responses(self) -> tuple[tuple[int, bytes], ...]:
        """(responding CAN ID, reassembled payload) for each answer, in order."""
        return tuple(self._responses)

    @property
    def nrc(self) -> int | None:
        return self._nrc

    @property
    def pending_extensions(self) -> int:
        return self._pending_extensions

    @property
    def done(self) -> bool:
        return self._state is not ExchangeState.PENDING


freeze(__name__)
