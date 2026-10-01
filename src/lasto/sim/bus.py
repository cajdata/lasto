"""A simulated CAN bus. Broadcasters, ECUs, testers, and interface channels exchange frames in time order."""

from __future__ import annotations

import heapq
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from lasto.sim.clock import FakeClock

Tap = Callable[[float, int, bytes], None]


class Node(Protocol):
    def on_frame(self, bus: SimBus, time: float, can_id: int, data: bytes) -> None:
        """React to a frame on the bus (for example by scheduling a response)."""


class Broadcaster:
    """A periodic frame, like the ones the truck's ECUs broadcast. While disabled it stays silent, on schedule."""

    def __init__(self, can_id: int, period: float, payload: Callable[[float], bytes]) -> None:
        self.can_id = can_id
        self.period = period
        self.payload = payload
        self.enabled = True


@dataclass(order=True)
class _Scheduled:
    time: float
    seq: int
    can_id: int = field(compare=False)
    data: bytes = field(compare=False)
    source: object = field(compare=False)
    broadcaster: Broadcaster | None = field(default=None, compare=False)
    action: Callable[[], None] | None = field(default=None, compare=False)


class SimBus:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self._queue: list[_Scheduled] = []
        self._seq = itertools.count()
        self._nodes: list[Node] = []
        self._taps: dict[int, tuple[object, Tap]] = {}
        self._broadcasters: list[Broadcaster] = []

    @property
    def broadcast_ids(self) -> frozenset[int]:
        return frozenset(b.can_id for b in self._broadcasters)

    def add_node(self, node: Node) -> None:
        self._nodes.append(node)

    def add_tap(self, owner: object, tap: Tap) -> None:
        self._taps[id(owner)] = (owner, tap)

    def remove_tap(self, owner: object) -> None:
        self._taps.pop(id(owner), None)

    def add_broadcaster(self, broadcaster: Broadcaster, offset: float = 0.0) -> None:
        self._broadcasters.append(broadcaster)
        self._push(self.clock.monotonic() + offset, broadcaster.can_id, b"", broadcaster, broadcaster)

    def schedule(self, delay: float, can_id: int, data: bytes, source: object) -> None:
        self._push(self.clock.monotonic() + delay, can_id, data, source)

    def schedule_at(self, time: float, can_id: int, data: bytes, source: object) -> None:
        """Schedule a frame at an absolute bus time (nodes answering a frame use the frame's time)."""
        self._push(time, can_id, data, source)

    def call_at(self, time: float, action: Callable[[], None]) -> None:
        """Run a scenario action at an absolute bus time, in order among the frames (a key switch, a stop request)."""
        heapq.heappush(self._queue, _Scheduled(time, next(self._seq), 0, b"", None, action=action))

    def awaiting_flow_control(self) -> frozenset[int]:
        """Request IDs of ECUs that have sent a first frame and are waiting for flow control."""
        return frozenset(
            node.request_id  # type: ignore[attr-defined]
            for node in self._nodes
            if getattr(node, "awaiting_flow_control", False)
        )

    def advance(self) -> None:
        """Put every frame due by now on the bus, in time order."""
        now = self.clock.monotonic()
        while self._queue and self._queue[0].time <= now:
            item = heapq.heappop(self._queue)
            if item.action is not None:
                item.action()
                continue
            data = item.data
            if item.broadcaster is not None:
                self._push(item.time + item.broadcaster.period, item.can_id, b"", item.source, item.broadcaster)
                if not item.broadcaster.enabled:
                    continue
                data = item.broadcaster.payload(item.time)
            self._emit(item.time, item.can_id, data, item.source)

    def transmit(self, can_id: int, data: bytes, source: object) -> None:
        """A frame written by an interface channel goes on the bus now."""
        self.advance()
        self._emit(self.clock.monotonic(), can_id, data, source)

    def _push(
        self, time: float, can_id: int, data: bytes, source: object, broadcaster: Broadcaster | None = None
    ) -> None:
        heapq.heappush(self._queue, _Scheduled(time, next(self._seq), can_id, data, source, broadcaster))

    def _emit(self, time: float, can_id: int, data: bytes, source: object) -> None:
        for owner, tap in list(self._taps.values()):
            if owner is not source:
                tap(time, can_id, data)
        for node in list(self._nodes):
            if node is not source:
                node.on_frame(self, time, can_id, data)
