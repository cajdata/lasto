"""Helpers shared by the tests."""

from __future__ import annotations

import sys
import types
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager

import pytest

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


class LooksLikeTheEngineId(int):
    """An ID crafted to compare equal to 0x7E0 while ctypes would write its real value."""

    def __eq__(self, other: object) -> bool:
        return other == 0x7E0 or int.__eq__(self, other)

    def __hash__(self) -> int:
        return hash(0x7E0)


def events(sink: MemoryAuditSink, name: str) -> list[dict[str, object]]:
    return [record for record in sink.records if record["event"] == name]


class WriteWatch:
    """Every call into the write function during a watch, and each caller that wasn't Gate._transmit."""

    def __init__(self) -> None:
        self.calls = 0
        self.strangers: list[str] = []


@contextmanager
def watching_the_write_function() -> Iterator[WriteWatch]:
    """At runtime, frames reach the write function only from the gate: watch who calls Writer.__call__.

    The structural test proves it from the source (test_structure.py); this watches whole simulated
    sessions do it. A profile hook sees each call into the Writer and the function that made it.
    """
    from lasto.safety.gate import Gate
    from lasto.safety.pcan_active import Writer

    write, transmit = Writer.__call__.__code__, Gate._transmit.__code__
    watch = WriteWatch()

    def profile(frame: types.FrameType, event: str, _arg: object) -> None:
        if event == "call" and frame.f_code is write:
            watch.calls += 1
            caller = frame.f_back.f_code  # type: ignore[union-attr]
            if caller is not transmit:
                watch.strangers.append(caller.co_qualname)

    previous = sys.getprofile()
    sys.setprofile(profile)
    try:
        yield watch
    finally:
        sys.setprofile(previous)


@contextmanager
def rewritten(entry: object, **fields: object) -> Iterator[None]:
    """Rewrite a frozen dataclass's fields in place, as calling its __init__ again used to (finding P1), then put
    them back. object.__setattr__ is the route pure Python can't block; the scanner bans it in src/."""
    saved = {name: getattr(entry, name) for name in fields}
    for name, value in fields.items():
        object.__setattr__(entry, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            object.__setattr__(entry, name, value)


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
        self.closed = False

    def close(self) -> None:
        self.closed = True

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
        self.drained: list[float] = []  # when the gate read the channel before sending
        self.on_drain: Callable[[], None] | None = None
        self.gate = Gate(
            self.writer,
            auditor,
            clock,
            self.interlocks,
            self.limiter,
            self.nrc,
            drain=self._drain,
            profile=profile,
            broadcast_ids=broadcast_ids,
        )
        if armed:
            self.gate.arm()
        REFUSALS.attach(auditor)

    def _drain(self) -> None:
        self.drained.append(self.clock.monotonic())
        if self.on_drain is not None:
            self.on_drain()

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


# ---- walking every attribute path from an object (the reachability tests) ----

# Not walked into: functions, methods, modules, classes, and plain values.
LEAVES = (
    types.FunctionType,
    types.BuiltinFunctionType,
    types.MethodType,
    types.ModuleType,
    type,
    str,
    bytes,
    int,
    float,
    bool,
    type(None),
)


def attributes(obj):
    if isinstance(obj, dict):
        return [(f"[{key!r}]", value) for key, value in list(obj.items())]
    if isinstance(obj, list | tuple | set | frozenset | deque):
        return [(f"[{index}]", value) for index, value in enumerate(list(obj))]
    found = [(f".{key}", value) for key, value in vars(obj).items()] if hasattr(obj, "__dict__") else []
    for cls in type(obj).__mro__:
        for slot in getattr(cls, "__slots__", ()):
            if isinstance(slot, str) and hasattr(obj, slot):
                found.append((f".{slot}", getattr(obj, slot)))
    return found


def paths_to(root, match):
    """Every attribute path from root to an object match() accepts, with the objects passed on the way."""
    hits, seen, queue = [], {id(root)}, deque([(root, "session", ())])
    while queue:
        obj, path, via = queue.popleft()
        for step, child in attributes(obj):
            if match(child):
                hits.append((path + step, via + (obj,), child))
                continue
            if isinstance(child, LEAVES) or id(child) in seen:
                continue
            seen.add(id(child))
            queue.append((child, path + step, via + (obj,)))
    return hits


def the_writer(session):
    hits = paths_to(session, lambda obj: type(obj).__name__ == "Writer")
    if not hits:
        pytest.fail("the polled session holds no armored write function")
    return hits[0][2]
