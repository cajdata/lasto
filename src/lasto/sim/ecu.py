"""Mock ECUs that answer diagnostic requests over ISO-TP, with scriptable misbehavior."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable

from lasto.sim.bus import SimBus

FUNCTIONAL_ID = 0x7DF
PAD = 0x00

Value = bytes | Callable[[], bytes]
# A scripted behavior for the next request: "silent", "busy" (NRC 0x21), "pending" (NRC 0x78, then
# the real answer), an int (that NRC), or bytes (sent as the response payload).
Behavior = str | int | bytes


def _value(value: Value) -> bytes:
    return value() if callable(value) else value


def _pad(raw: bytes) -> bytes:
    return raw + bytes([PAD]) * (8 - len(raw))


class MockEcu:
    def __init__(
        self,
        name: str,
        *,
        request_id: int,
        response_id: int,
        pids: dict[int, Value] | None = None,
        local_ids: dict[int, Value] | None = None,
        dtcs: Iterable[int] = (),
        vin: str = "JTJBT20X060000001",
        response_delay: float = 0.004,
        frame_gap: float = 0.001,
    ) -> None:
        self.name = name
        self.request_id = request_id
        self.response_id = response_id
        self.pids = dict(pids or {})
        self.local_ids = dict(local_ids or {})
        self.dtcs = list(dtcs)
        self.vin = vin
        self.response_delay = response_delay
        self.frame_gap = frame_gap
        self.script: deque[Behavior] = deque()
        self.received: list[bytes] = []
        self.awaiting_flow_control = False
        self._remaining = b""
        self._sequence = 1
        self._time = 0.0

    def on_frame(self, bus: SimBus, time: float, can_id: int, data: bytes) -> None:
        if not data:
            return
        self._time = time
        frame_type = data[0] >> 4
        if frame_type == 3 and can_id == self.request_id and self.awaiting_flow_control:
            self._send_consecutive(bus)
            return
        if frame_type != 0 or can_id not in (self.request_id, FUNCTIONAL_ID):
            return
        payload = bytes(data[1 : 1 + (data[0] & 0x0F)])
        if not payload or (can_id == FUNCTIONAL_ID and payload[0] > 0x0A):
            return
        self.received.append(payload)
        behavior = self.script.popleft() if self.script else None
        response = self._behave(bus, payload, behavior, functional=can_id == FUNCTIONAL_ID)
        if response is not None:
            self.send(bus, response, self.response_delay)

    def _behave(self, bus: SimBus, payload: bytes, behavior: Behavior | None, *, functional: bool) -> bytes | None:
        service = payload[0]
        if behavior is None:
            return self.answer(payload, functional=functional)
        if behavior == "silent":
            return None
        if behavior == "busy":
            return bytes([0x7F, service, 0x21])
        if behavior == "pending":
            answer = self.answer(payload, functional=functional)
            if answer is not None:
                self.send(bus, answer, self.response_delay + 0.3)
            return bytes([0x7F, service, 0x78])
        if isinstance(behavior, int):
            return bytes([0x7F, service, behavior])
        return bytes(behavior)  # type: ignore[arg-type]

    def answer(self, payload: bytes, *, functional: bool = False) -> bytes | None:
        service = payload[0]
        if service == 0x01:
            out = bytearray([0x41])
            for pid in payload[1:]:
                if pid in self.pids:
                    out += bytes([pid]) + _value(self.pids[pid])
            return bytes(out) if len(out) > 1 else None
        if service == 0x09 and payload[1:2] == b"\x02":
            return b"\x49\x02\x01" + self.vin.encode("ascii")
        if service in (0x03, 0x07, 0x0A):
            body = b"".join(code.to_bytes(2, "big") for code in self.dtcs)
            return bytes([service + 0x40, len(self.dtcs)]) + body
        if functional:
            return None
        if service == 0x21 and len(payload) == 2 and payload[1] in self.local_ids:
            return bytes([0x61, payload[1]]) + _value(self.local_ids[payload[1]])
        if service == 0x1A and len(payload) == 2:
            return bytes([0x5A, payload[1]]) + self.name.upper().encode("ascii")
        return bytes([0x7F, service, 0x31 if service in (0x21, 0x22) else 0x11])

    def send(self, bus: SimBus, payload: bytes, delay: float) -> None:
        """Send a response payload, timed from the frame being answered: a single frame, or a first frame
        that waits for flow control."""
        if len(payload) <= 7:
            bus.schedule_at(self._time + delay, self.response_id, _pad(bytes([len(payload)]) + payload), self)
            return
        first = bytes([0x10 | (len(payload) >> 8), len(payload) & 0xFF]) + payload[:6]
        self._remaining = payload[6:]
        self._sequence = 1
        self.awaiting_flow_control = True
        bus.schedule_at(self._time + delay, self.response_id, first, self)

    def _send_consecutive(self, bus: SimBus) -> None:
        self.awaiting_flow_control = False
        delay = self.frame_gap
        remaining = self._remaining
        while remaining:
            chunk, remaining = remaining[:7], remaining[7:]
            bus.schedule_at(self._time + delay, self.response_id, _pad(bytes([0x20 | self._sequence]) + chunk), self)
            self._sequence = (self._sequence + 1) & 0x0F
            delay += self.frame_gap
        self._remaining = b""
