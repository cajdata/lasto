"""Helpers shared by the tests."""

from __future__ import annotations

from collections.abc import Iterable

from lasto.safety import ecus
from lasto.safety.audit import REFUSALS, Auditor, MemoryAuditSink
from lasto.safety.errors import InterfaceError
from lasto.safety.frames import CanFrame
from lasto.safety.gate import Gate
from lasto.safety.interlocks import Interlocks
from lasto.safety.killswitch import KILL_SWITCH, NrcMonitor
from lasto.safety.ratelimit import RateLimiter
from lasto.safety.requests import Purpose, Request, interlock_probe, read_pid
from lasto.safety.session import PolledSession, open_polled_session
from lasto.sim.clock import FakeClock
from lasto.sim.vehicle import Sim

CHANNEL = "PCAN_USBBUS1"
HANDLE = 0x51
LOGGING_RPM_SPEED = read_pid([0x0C, 0x0D], purpose=Purpose.LOGGING, ecu=ecus.ENGINE)


def events(sink: MemoryAuditSink, name: str) -> list[dict[str, object]]:
    return [record for record in sink.records if record["event"] == name]


def frame(can_id: int, *data: int) -> CanFrame:
    return CanFrame(can_id, bytes(data) + bytes(8 - len(data)), 0)


class StubWriter:
    """Stands in for the write function, so gate tests see exactly what the gate lets through, and when."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.frames: list[tuple[int, bytes]] = []
        self.times: list[float] = []
        self.fail: InterfaceError | None = None
        self.duration = 0.0  # how long each write takes

    def __call__(self, can_id: int, data: bytes, *, purpose: str, kind: str) -> None:
        if self.fail is not None:
            raise self.fail
        self.frames.append((can_id, bytes(data)))
        self.times.append(self.clock.monotonic())
        self.clock.advance(self.duration)


class GateHarness:
    def __init__(
        self,
        clock: FakeClock,
        auditor: Auditor,
        *,
        profile: Iterable[Request] = (LOGGING_RPM_SPEED,),
        broadcast_ids: Iterable[int] = (),
        armed: bool = True,
    ) -> None:
        self.clock = clock
        self.killswitch = KILL_SWITCH  # the process kill switch; the test plugin clears it before each test
        self.interlocks = Interlocks()
        self.limiter = RateLimiter()
        self.nrc = NrcMonitor()
        self.writer = StubWriter(clock)
        self.gate = Gate(
            self.writer,
            auditor,
            clock,
            self.interlocks,
            self.limiter,
            self.nrc,
            profile=profile,
            broadcast_ids=broadcast_ids,
        )
        if armed:
            self.gate.arm()
        REFUSALS.attach(auditor)

    def park(self, *, volts: float = 12.6, rpm: float = 0.0) -> None:
        now = self.clock.monotonic()
        self.interlocks.update_speed(0, now, source="poll")
        self.interlocks.update_voltage(volts, now)
        self.interlocks.update_rpm(rpm, now)


def open_polled(sim: Sim, auditor: Auditor, *, profile: Iterable[Request] = (LOGGING_RPM_SPEED,), **kwargs: object) -> PolledSession:
    kwargs.setdefault("listen_seconds", 0.05)
    return open_polled_session(CHANNEL, profile=profile, auditor=auditor, clock=sim.clock, library=sim.dll, **kwargs)


def park(session: PolledSession) -> None:
    """Read speed and voltage so parked-only requests pass the interlocks."""
    session.request(interlock_probe(0x42))
    session.request(interlock_probe(0x0D))
