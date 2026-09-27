"""Opening capture sessions. The only place a transmit-capable (polled) channel is created.

A passive session has no gate and no transmit path at all. A polled session
listens for a moment before it may transmit: if another tester is talking,
or anything shows up on a request ID, the kill switch trips and the session
refuses to start. While a session is open its auditor receives every
refusal raised anywhere in the safety core; a session that refuses to open
logs why.
"""

from __future__ import annotations

from collections.abc import Iterable

from lasto.safety.audit import REFUSALS, Auditor
from lasto.safety.clock import Clock
from lasto.safety.frames import Received
from lasto.safety.gate import Exchange, ExchangeState, Gate
from lasto.safety.interlocks import Interlocks
from lasto.safety.killswitch import KillSwitch, NrcMonitor
from lasto.safety.pcan_active import ActiveChannel, TransmitPcan, open_active
from lasto.safety.pcan_dll import ReadOnlyPcan
from lasto.safety.pcan_passive import PassiveChannel, open_passive
from lasto.safety.ratelimit import RateLimiter
from lasto.safety.reader import Reader, Subscriber
from lasto.safety.requests import Request

LISTEN_WINDOW = 2.0
PUMP_INTERVAL = 0.002


def _refused(auditor: Auditor, mode: str, channel_name: str, error: BaseException) -> None:
    """Log why a session refused to open, and stop sending it refusals."""
    try:
        auditor.event(
            "session_refused",
            mode=mode,
            channel=channel_name,
            reason=getattr(error, "reason", type(error).__name__),
            detail=str(error),
        )
    finally:
        REFUSALS.detach(auditor)


class PassiveSession:
    """Listen-only capture."""

    def __init__(self, channel: PassiveChannel, reader: Reader, auditor: Auditor) -> None:
        self._channel = channel
        self._reader = reader
        self._auditor = auditor

    @property
    def reader(self) -> Reader:
        return self._reader

    def pump(self) -> list[Received]:
        return self._reader.poll_once()

    def close(self) -> None:
        self._channel.close()
        self._auditor.event("session_closed", mode="passive")
        REFUSALS.detach(self._auditor)


def open_passive_session(
    channel_name: str,
    *,
    auditor: Auditor,
    clock: Clock,
    pcan: ReadOnlyPcan | None = None,
    subscribers: Iterable[Subscriber] = (),
) -> PassiveSession:
    REFUSALS.attach(auditor)
    try:
        channel = open_passive(channel_name, pcan=pcan)
    except BaseException as exc:
        _refused(auditor, "passive", channel_name, exc)
        raise
    try:
        auditor.event("session_opened", mode="passive", listen_only_confirmed=True, **channel.describe())
    except BaseException:
        channel.close()
        REFUSALS.detach(auditor)
        raise
    return PassiveSession(channel, Reader(channel, clock, subscribers=subscribers), auditor)


class PolledSession:
    """Normal-mode capture that can send the allowlisted reads of a typed request."""

    def __init__(
        self,
        channel: ActiveChannel,
        gate: Gate,
        reader: Reader,
        killswitch: KillSwitch,
        auditor: Auditor,
        clock: Clock,
    ) -> None:
        self._channel = channel
        self._gate = gate
        self._reader = reader
        self._killswitch = killswitch
        self._auditor = auditor
        self._clock = clock

    @property
    def killswitch(self) -> KillSwitch:
        return self._killswitch

    @property
    def reader(self) -> Reader:
        return self._reader

    def pump(self) -> list[Received]:
        items = self._reader.poll_once()
        self._gate.poll()
        return items

    def request(self, request: Request) -> Exchange:
        """Send one request through the gate and wait for its outcome."""
        exchange = self._gate.submit(request)
        while exchange.state is ExchangeState.PENDING:
            self.pump()
            if exchange.state is ExchangeState.PENDING:
                self._clock.sleep(PUMP_INTERVAL)
        return exchange

    def close(self) -> None:
        self._channel.close()
        self._auditor.event("session_closed", mode="polled", kill_cause=self._killswitch.cause)
        REFUSALS.detach(self._auditor)

    def _on_kill(self, cause: str) -> None:
        with self._gate.lock:
            self._gate.abort()
            confirmed = self._channel.enter_listen_only()
            self._auditor.event("kill_listen_only", cause=cause, listen_only_confirmed=confirmed)


def open_polled_session(
    channel_name: str,
    *,
    profile: Iterable[Request],
    auditor: Auditor,
    clock: Clock,
    pcan: TransmitPcan | None = None,
    broadcast_ids: Iterable[int] = (),
    subscribers: Iterable[Subscriber] = (),
    listen_seconds: float = LISTEN_WINDOW,
) -> PolledSession:
    REFUSALS.attach(auditor)
    channel: ActiveChannel | None = None
    try:
        channel = open_active(channel_name, pcan=pcan)
        killswitch = KillSwitch(auditor)
        gate = Gate(
            channel,
            auditor,
            clock,
            killswitch,
            Interlocks(),
            RateLimiter(),
            NrcMonitor(killswitch),
            profile=profile,
            broadcast_ids=broadcast_ids,
        )
        reader = Reader(channel, clock, killswitch=killswitch, subscribers=(gate.on_frame, *subscribers))
        session = PolledSession(channel, gate, reader, killswitch, auditor, clock)
        killswitch.add_listener(session._on_kill)
        auditor.event("session_opened", mode="polled", **channel.describe())
        deadline = clock.monotonic() + listen_seconds
        while clock.monotonic() < deadline and not killswitch.tripped:
            session.pump()
            clock.sleep(PUMP_INTERVAL)
        killswitch.check(request=f"open a polled session on {channel_name}")
        gate.arm()
    except BaseException as exc:
        if channel is not None:
            channel.close()
        _refused(auditor, "polled", channel_name, exc)
        raise
    return session
