"""Another scan tool on the bus (like the Creader), sending its own requests."""

from __future__ import annotations

from lasto.sim.bus import SimBus


class SimTester:
    def __init__(self, bus: SimBus) -> None:
        self.bus = bus

    def request(self, can_id: int, payload: bytes, delay: float = 0.0) -> None:
        raw = bytes([len(payload)]) + payload
        self.bus.schedule(delay, can_id, raw + bytes(8 - len(raw)), self)
