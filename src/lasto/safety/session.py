"""Opening capture sessions. The only place a transmit-capable (polled) channel is created.

A passive session has no gate and no transmit path at all. A polled session
listens for a moment before it may transmit: if another tester is talking,
or anything shows up on a request ID, the kill switch trips and the session
refuses to start. While a session is open its auditor receives every
refusal raised anywhere in the safety core; a session that refuses to open
logs why.

The gate and the transmit binding are imported inside open_polled_session,
so this module never carries a name that can transmit. The write function
open_active returns goes to the gate and nowhere else.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from lasto.safety._frozen import SealedType, freeze
from lasto.safety.audit import REFUSALS, Auditor, refuse
from lasto.safety.clock import Clock, require_system_clock
from lasto.safety.errors import InterfaceError, PassiveModeUnconfirmed, SafetyViolation
from lasto.safety.exchange import Exchange, ExchangeState
from lasto.safety.frames import Received
from lasto.safety.interlocks import Interlocks
from lasto.safety.killswitch import KILL_SWITCH, KillSwitch, NrcMonitor
from lasto.safety.pcan_dll import ReadOnlyPcan, load_readonly
from lasto.safety.pcan_passive import PassiveChannel, open_passive
from lasto.safety.ratelimit import RateLimiter
from lasto.safety.reader import Reader, Subscriber
from lasto.safety.requests import Request

if TYPE_CHECKING:
    from lasto.safety.gate import Gate
    from lasto.safety.pcan_active import ActiveChannel

LISTEN_WINDOW = 2.0
PUMP_INTERVAL = 0.002
# A passive channel that can't be trusted is reopened from scratch: a few attempts per incident, a
# pause between them (a USB replug takes seconds), and a cap per session so a flapping adapter can't
# cycle forever.
REOPEN_ATTEMPTS = 3
REOPEN_DELAY = 2.0
MAX_REOPENS_PER_SESSION = 10


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


class PassiveSession(metaclass=SealedType):
    """Listen-only capture that never trusts a channel that changed under it.

    The reader re-checks listen-only continuously. If it ever reads anything
    but ON, or the channel fails or resets, the session logs why and
    uninitializes the channel, which discards whatever the driver resumed on
    its own. Then it runs the full open_passive sequence again (set
    listen-only before initializing, read it back). If that doesn't work
    within a few attempts, the session ends and logs why.
    """

    __slots__ = (
        "_auditor", "_channel", "_channel_name", "_clock", "_end_reason", "_pcan", "_reader", "_reopens", "_subscribers",
    )  # fmt: skip

    def __init__(
        self,
        channel_name: str,
        pcan: ReadOnlyPcan,
        channel: PassiveChannel,
        auditor: Auditor,
        clock: Clock,
        subscribers: Iterable[Subscriber],
    ) -> None:
        self._channel_name = channel_name
        self._pcan = pcan
        self._channel = channel
        self._auditor = auditor
        self._clock = clock
        self._subscribers = tuple(subscribers)
        self._reader = self._new_reader(channel)
        self._reopens = 0
        self._end_reason: str | None = None

    def _new_reader(self, channel: PassiveChannel) -> Reader:
        return Reader(channel, self._clock, subscribers=self._subscribers, verify_listen_only=True, auditor=self._auditor)

    @property
    def reader(self) -> Reader:
        return self._reader

    @property
    def ended(self) -> bool:
        return self._end_reason is not None

    @property
    def end_reason(self) -> str | None:
        return self._end_reason

    @property
    def reopens(self) -> int:
        return self._reopens

    def pump(self) -> list[Received]:
        if self.ended:
            return []
        items = self._reader.poll_once()
        if self._reader.failed:
            self._distrust(self._reader.failure_reason, self._reader.failed_status)  # type: ignore[arg-type]
        return items

    def _distrust(self, reason: str, status: int | None) -> None:
        self._auditor.event(
            "passive_channel_distrusted",
            channel=self._channel_name,
            reason=reason,
            status=None if status is None else f"0x{status:X}",
            detail="closing the channel; the driver's automatic resume is never trusted",
        )
        if not self._channel.close():
            # Still initialized, maybe on the bus: nothing is reopened on top of it (finding #5).
            self._end(f"could not close the channel after {reason}; it may still be on the bus")
            return
        for attempt in range(1, REOPEN_ATTEMPTS + 1):
            if self._reopens >= MAX_REOPENS_PER_SESSION:
                self._end(f"reopened {self._reopens} times already; the channel keeps losing listen-only or failing")
                return
            self._clock.sleep(REOPEN_DELAY)
            try:
                channel = open_passive(self._channel_name, pcan=self._pcan)
            except (InterfaceError, PassiveModeUnconfirmed) as exc:
                self._auditor.event(
                    "passive_reopen_failed", attempt=attempt, reason=type(exc).__name__, detail=str(exc)
                )
                continue
            self._reopens += 1
            self._channel = channel
            self._reader = self._new_reader(channel)
            self._auditor.event(
                "passive_channel_reopened", attempt=attempt, listen_only_confirmed=True, **channel.describe()
            )
            return
        self._end(f"could not reopen the channel after {reason}")

    def _end(self, reason: str) -> None:
        self._end_reason = reason
        self._auditor.event("session_ended", mode="passive", reason=reason)
        REFUSALS.detach(self._auditor)

    def close(self) -> None:
        if self.ended:
            return
        uninitialized = self._channel.close()
        self._end_reason = "closed"
        self._auditor.event("session_closed", mode="passive", channel_uninitialized=uninitialized)
        REFUSALS.detach(self._auditor)


def open_passive_session(
    channel_name: str,
    *,
    auditor: Auditor,
    clock: Clock,
    library: object | None = None,
    subscribers: Iterable[Subscriber] = (),
) -> PassiveSession:
    """Open a listen-only capture on the real DLL, or on a stand-in `library` such as the simulator's.

    On real hardware the clock must be SystemClock: listen-only re-checks are timed by it.
    """
    REFUSALS.attach(auditor)
    try:
        if library is None:
            require_system_clock(clock, request=f"open a passive session on {channel_name}")
        pcan = load_readonly(library)
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
    return PassiveSession(channel_name, pcan, channel, auditor, clock, subscribers)


class PolledSession(metaclass=SealedType):
    """Normal-mode capture that can send the allowlisted reads of a typed request.

    After a kill, capture goes on in listen-only, re-checked on every status
    check. The channel is closed (uninitialized) instead if listen-only can't
    be confirmed after the kill, if it is lost later, or if the interface
    fails, so the driver can never carry on with it in normal mode (finding B).
    """

    __slots__ = ("_auditor", "_channel", "_channel_closed", "_clock", "_gate", "_reader")

    def __init__(self, channel: ActiveChannel, gate: Gate, reader: Reader, auditor: Auditor, clock: Clock) -> None:
        self._channel = channel
        self._gate = gate
        self._reader = reader
        self._auditor = auditor
        self._clock = clock
        self._channel_closed = False

    @property
    def killswitch(self) -> KillSwitch:
        """The process kill switch, shared by every session. Anyone may trip it; nothing resets it."""
        return KILL_SWITCH

    @property
    def reader(self) -> Reader:
        return self._reader

    def pump(self) -> list[Received]:
        if self._channel_closed:
            return []
        items = self._reader.poll_once()
        if self._reader.failed:
            self._close_channel(self._reader.failure_reason)  # type: ignore[arg-type]
            return items
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
        KILL_SWITCH.remove_listener(self._on_kill)
        uninitialized = self._channel.close()
        self._auditor.event(
            "session_closed", mode="polled", kill_cause=KILL_SWITCH.cause, channel_uninitialized=uninitialized
        )
        REFUSALS.detach(self._auditor)

    def _on_kill(self, cause: str) -> None:
        with self._gate.lock:
            self._gate.abort()
            confirmed = self._channel.enter_listen_only()
            self._auditor.event("kill_listen_only", cause=cause, listen_only_confirmed=confirmed)
            if confirmed:
                self._reader.verify_listen_only()
            else:
                self._close_channel("listen_only_not_confirmed_after_kill")

    def _close_channel(self, reason: str) -> None:
        """Uninitialize the channel and stop capture: it can't be trusted to stay off the bus."""
        if self._channel_closed:
            return
        self._channel_closed = True
        uninitialized = self._channel.close()
        self._auditor.event(
            "polled_channel_closed", reason=reason, kill_cause=KILL_SWITCH.cause, uninitialized=uninitialized
        )


def open_polled_session(
    channel_name: str,
    *,
    profile: Iterable[Request],
    auditor: Auditor,
    clock: Clock,
    library: object | None = None,
    broadcast_ids: Iterable[int] = (),
    subscribers: Iterable[Subscriber] = (),
    listen_seconds: float = LISTEN_WINDOW,
) -> PolledSession:
    """Open a normal-mode capture that may send typed requests, on the real DLL or a stand-in `library`.

    Refused, before the adapter is touched, once the process kill switch has tripped.
    """
    from lasto.safety.gate import Gate
    from lasto.safety.pcan_active import open_active

    REFUSALS.attach(auditor)
    channel: ActiveChannel | None = None
    session: PolledSession | None = None
    try:
        KILL_SWITCH.check(request=f"open a polled session on {channel_name}")
        if library is None:
            # Real hardware: every timing rule runs on the system clock (finding N2).
            require_system_clock(clock, request=f"open a polled session on {channel_name}")
        if library is None and not (type(listen_seconds) in (int, float) and listen_seconds >= LISTEN_WINDOW):
            # On real hardware the listen for other testers can't be skipped or shortened (finding D).
            refuse(
                SafetyViolation(
                    "listen_window_too_short", f"{listen_seconds!r} s; on real hardware it is at least {LISTEN_WINDOW:g} s"
                ),
                transport="pcan",
                request=f"open a polled session on {channel_name}",
            )
        broadcast = frozenset(broadcast_ids)
        channel, writer = open_active(channel_name, auditor=auditor, clock=clock, library=library, broadcast_ids=broadcast)
        reader = Reader(channel, clock, trips_kill_switch=True)
        gate = Gate(
            writer,
            auditor,
            clock,
            Interlocks(),
            RateLimiter(),
            NrcMonitor(),
            drain=lambda: reader.poll_once(status_now=True),
            profile=profile,
            broadcast_ids=broadcast,
        )
        for subscriber in (gate.on_frame, *subscribers):
            reader.subscribe(subscriber)
        session = PolledSession(channel, gate, reader, auditor, clock)
        KILL_SWITCH.add_listener(session._on_kill)
        auditor.event("session_opened", mode="polled", **channel.describe())
        deadline = clock.monotonic() + listen_seconds
        while clock.monotonic() < deadline and not KILL_SWITCH.tripped:
            session.pump()
            clock.sleep(PUMP_INTERVAL)
        KILL_SWITCH.check(request=f"open a polled session on {channel_name}")
        gate.arm()
    except BaseException as exc:
        if session is not None:
            KILL_SWITCH.remove_listener(session._on_kill)
        if channel is not None:
            channel.close()
        _refused(auditor, "polled", channel_name, exc)
        raise
    return session


freeze(__name__)
