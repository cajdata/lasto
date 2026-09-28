"""The transmit gate: the only path from a typed request to the CAN bus.

Every frame lasto sends in polled mode goes through Gate._transmit. It makes
the checks that need state (a flow control frame must answer the first frame
that is waiting for it), re-checks the exact bytes against the policy, and
hands them to the write function, the one object that holds CAN_Write. That
function checks the bytes again, checks the kill switch, and records them in
the audit log before they go out (lasto.safety.pcan_active). Requests arrive
as typed Request objects from lasto.safety.requests; nothing here accepts raw
bytes or CAN IDs from callers. Every refusal is audited where it is raised
(rule 11).

The gate also watches every received frame. A frame on a request ID that
lasto didn't send (another tester, or broadcast traffic), a diagnostic
response nobody asked for, a malformed response, or a flow control frame
from an ECU all trip the kill switch. A late answer to the previous request
is ignored and logged.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterable
from typing import Protocol

from lasto.safety import ecus, isotp, policy
from lasto.safety._frozen import SealedProtocolType, SealedType, freeze
from lasto.safety.audit import Auditor, refuse
from lasto.safety.clock import Clock
from lasto.safety.errors import InterfaceError, SafetyError, SafetyViolation
from lasto.safety.exchange import Exchange, ExchangeState
from lasto.safety.frames import CanFrame, Received
from lasto.safety.interlocks import Interlocks
from lasto.safety.killswitch import KILL_SWITCH, NrcMonitor
from lasto.safety.ratelimit import RateLimiter
from lasto.safety.requests import Purpose, Request

# ISO 15765-4 timing, with some margin for a Windows laptop.
P2_TIMEOUT = 0.15
P2_STAR_TIMEOUT = 5.0
CONSECUTIVE_FRAME_TIMEOUT = 0.25
MAX_RESPONSE_PENDING = 10
# The gate never waits longer than this for a rate-limit slot; the caller retries later.
MAX_RATE_WAIT = 1.0
# While it waits for a slot, the gate reads the channel at least this often, so a kill is acted on at once.
WAIT_SLICE = 0.02
# A slow answer to the previous request isn't mistaken for another tester.
LATE_RESPONSE_GRACE = 1.0

NEGATIVE_RESPONSE = 0x7F
NRC_BUSY_REPEAT_REQUEST = 0x21
NRC_RESPONSE_PENDING = 0x78
POSITIVE_RESPONSE_OFFSET = 0x40


class WriteFunction(Protocol, metaclass=SealedProtocolType):
    def __call__(self, can_id: int, data: bytes, *, purpose: str, kind: str) -> None:
        """Check one 8-byte standard frame, audit it, and put it on the bus; or refuse, or raise InterfaceError."""


def _describe(request: object) -> str:
    return request.describe() if isinstance(request, Request) else type(request).__name__


_hex_id = policy.hex_id


def _target_key(request: Request) -> str:
    return "functional" if request.target is None else request.target.name


def _deny(reason: str, detail: str, request: str) -> None:
    refuse(SafetyViolation(reason, detail), transport="pcan", request=request)


class Gate(metaclass=SealedType):
    __slots__ = (
        "_armed", "_auditor", "_broadcast_ids", "_clock", "_drain", "_interlocks", "_last", "_last_finished",
        "_limiter", "_lock", "_nrc_monitor", "_pending", "_profile", "_request_ids", "_writer",
    )  # fmt: skip

    def __init__(
        self,
        writer: WriteFunction,
        auditor: Auditor,
        clock: Clock,
        interlocks: Interlocks,
        limiter: RateLimiter,
        nrc_monitor: NrcMonitor,
        *,
        drain: Callable[[], object],
        profile: Iterable[Request],
        broadcast_ids: Iterable[int] = (),
    ) -> None:
        """`drain` reads everything the channel has received and checks the bus status now (the polled reader)."""
        keys = set()
        for request in profile:
            if not isinstance(request, Request) or request.purpose is not Purpose.LOGGING:
                refuse(
                    ValueError("a logging profile holds logging requests made with lasto.safety.requests"),
                    transport="pcan",
                    request=_describe(request),
                    reason="bad_logging_profile",
                )
            keys.add(request.key)
        self._profile = frozenset(keys)
        self._writer = writer
        self._drain = drain
        self._auditor = auditor
        self._clock = clock
        self._interlocks = interlocks
        self._limiter = limiter
        self._nrc_monitor = nrc_monitor
        self._broadcast_ids = frozenset(broadcast_ids)
        self._request_ids = policy.request_ids()
        self._lock = threading.RLock()
        self._armed = False
        self._pending: Exchange | None = None
        self._last: Exchange | None = None
        self._last_finished = float("-inf")

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def pending(self) -> Exchange | None:
        return self._pending

    def arm(self) -> None:
        """Allow requests. The session calls this after its listen window found no other tester."""
        with self._lock:
            self._armed = True

    def abort(self) -> None:
        """End any outstanding exchange (the kill switch calls this)."""
        with self._lock:
            if self._pending is not None:
                self._finish(self._pending, ExchangeState.ABORTED, self._clock.monotonic())

    # ---- sending ----

    def submit(self, request: Request) -> Exchange:
        with self._lock:
            return self._submit(request)

    def _submit(self, request: Request) -> Exchange:
        text = _describe(request)
        KILL_SWITCH.check(request=text)
        if not self._armed:
            _deny("gate_not_armed", "the polled session hasn't finished its listen window", text)
        if not isinstance(request, Request):
            _deny("not_a_typed_request", type(request).__name__, text)
        if self._pending is not None:
            _deny("request_outstanding", "wait for the previous request to finish", text)
        target = request.target
        if target is not None and not ecus.is_approved(target):
            _deny("ecu_not_approved", getattr(target, "name", _hex_id(target)), text)  # a forged target may be anything
        now = self._clock.monotonic()
        self._interlocks.check(request, now, self._profile)
        can_id = policy.FUNCTIONAL_REQUEST_ID if target is None else target.request_id
        data = isotp.encode_single_frame(
            request.payload, ext_address=None if target is None else target.ext_address, padding=policy.PADDING_BYTE
        )
        self._check(can_id, data, "request", policy.frame_text(can_id, data))
        wait = self._limiter.delay(request.purpose, now)
        if wait > MAX_RATE_WAIT:
            _deny("rate_limited", f"next slot in {wait:.3f} s", text)
        self._read_before_sending(wait, text)
        now = self._clock.monotonic()
        self._interlocks.check(request, now, self._profile)  # again: the readings may have aged while waiting
        self._transmit(can_id, data, purpose=request.purpose.value, kind="request")
        self._limiter.commit(request.purpose, self._clock.monotonic())  # spacing counts from the end of the write
        exchange = Exchange(request, can_id, sent_at=now, deadline=now + P2_TIMEOUT)
        self._pending = exchange
        return exchange

    def _read_before_sending(self, wait: float, text: str) -> None:
        """Wait out the rate-limit slot in short slices, reading the channel after each; stop at once on a kill.

        The last read comes right before the frame goes out, so every kill trigger that has already
        arrived (an error frame, a bus-state change, another tester) is seen first (finding A).
        """
        slices = math.ceil(wait / WAIT_SLICE) if wait > 0 else 1
        pause = wait / slices if wait > 0 else 0.0
        for _ in range(slices):
            if pause:
                self._clock.sleep(pause)
            self._drain()
            KILL_SWITCH.check(request=text)

    def _check(self, can_id: int, data: bytes, kind: str, text: str) -> None:
        """The policy's stateless check on the exact bytes, and that they are the kind of frame intended."""
        checked = policy.check_frame(can_id, data, broadcast_ids=self._broadcast_ids)
        if checked != kind:
            _deny("frame_kind_mismatch", f"a {checked} frame sent as {kind!r}", text)

    def _check_flow_control_wanted(self, can_id: int, text: str) -> None:
        """Flow control only answers the first frame waiting for it, once, on that ECU's request ID."""
        pending = self._pending
        if pending is None or pending._rx_id is None or pending._flow_control_sent:
            _deny("flow_control_unsolicited", "no first frame is waiting for flow control", text)
        ecu = ecus.by_response_id(pending._rx_id)  # type: ignore[union-attr,arg-type]
        if ecu is None or can_id != ecu.request_id:
            _deny("flow_control_wrong_id", _hex_id(can_id), text)

    def _transmit(self, can_id: int, data: bytes, *, purpose: str, kind: str) -> None:
        """The gate's last check before the wire. Every frame lasto sends passes here, then the write function."""
        text = policy.frame_text(can_id, data)
        if kind == "flow_control":
            self._check_flow_control_wanted(can_id, text)
        elif kind != "request":
            _deny("unknown_frame_kind", repr(kind), text)
        self._check(can_id, data, kind, text)
        KILL_SWITCH.check(request=text)
        try:
            self._writer(can_id, data, purpose=purpose, kind=kind)
        except InterfaceError as exc:
            self._trip("interface_write_failed", str(exc))
            raise

    # ---- receiving ----

    def on_frame(self, item: Received) -> None:
        """Reader subscriber: sees every item read from the channel."""
        if not isinstance(item, CanFrame) or item.extended or item.rtr:
            return
        with self._lock:
            now = self._clock.monotonic()
            can_id = item.can_id
            if can_id in self._request_ids:
                self._trip("foreign_tester", f"a frame on request ID {_hex_id(can_id)} that lasto didn't send")
                return
            if can_id not in ecus.FUNCTIONAL_RESPONSE_IDS and ecus.by_response_id(can_id) is None:
                return
            pending = self._pending
            if pending is not None and self._responder_matches(pending, can_id):
                self._handle_response(pending, item, now)
            elif self._is_late(can_id, now):
                self._auditor.event(
                    "late_response_ignored",
                    can_id=_hex_id(can_id),
                    data=item.data.hex(" ").upper(),
                    seconds_after_last_request_ended=round(now - self._last_finished, 3),
                )
            else:
                self._trip("unexpected_response", f"diagnostic response on {_hex_id(can_id)} with no matching request")

    def poll(self) -> None:
        """Finish an exchange whose deadline has passed."""
        with self._lock:
            pending = self._pending
            now = self._clock.monotonic()
            if pending is None or now < pending.deadline:
                return
            if pending.can_id == policy.FUNCTIONAL_REQUEST_ID and pending._responses and pending._rx_id is None:
                self._finish(pending, ExchangeState.DONE, now)
            else:
                self._finish_timeout(pending, now)

    def _responder_matches(self, exchange: Exchange, can_id: int) -> bool:
        if exchange.can_id == policy.FUNCTIONAL_REQUEST_ID:
            return can_id in ecus.FUNCTIONAL_RESPONSE_IDS
        return exchange.request.target is not None and can_id == exchange.request.target.response_id

    def _is_late(self, can_id: int, now: float) -> bool:
        last = self._last
        return last is not None and now - self._last_finished <= LATE_RESPONSE_GRACE and self._responder_matches(last, can_id)

    def _handle_response(self, pending: Exchange, frame: CanFrame, now: float) -> None:
        ecu = ecus.by_response_id(frame.can_id)
        try:
            parsed = isotp.parse(frame.data, ext_address=None if ecu is None else ecu.ext_address)
        except isotp.IsoTpError as exc:
            self._trip("malformed_response", str(exc))
            return
        if parsed.kind is isotp.FrameKind.SINGLE:
            self._handle_payload(pending, frame.can_id, parsed.payload, now)
        elif parsed.kind is isotp.FrameKind.FIRST:
            self._handle_first_frame(pending, frame.can_id, parsed, now)
        elif parsed.kind is isotp.FrameKind.CONSECUTIVE:
            self._handle_consecutive_frame(pending, frame.can_id, parsed, now)
        else:
            self._trip("unexpected_flow_control", "an ECU sent flow control, but lasto never sends first frames")

    def _handle_first_frame(self, pending: Exchange, can_id: int, parsed: isotp.IsoTpFrame, now: float) -> None:
        if pending._rx_id is not None:
            self._auditor.event(
                "response_abandoned",
                can_id=_hex_id(can_id),
                detail=f"already receiving a multi-frame answer from {_hex_id(pending._rx_id)}; no flow control",
            )
            return
        ecu = ecus.by_response_id(can_id)
        if ecu is None:
            self._auditor.event(
                "response_abandoned", can_id=_hex_id(can_id), detail="multi-frame response from an unapproved ECU"
            )
            return
        pending._rx_id = can_id
        pending._rx_length = parsed.length
        pending._rx_data = bytearray(parsed.payload)
        pending._rx_sequence = 1
        pending._deadline = now + CONSECUTIVE_FRAME_TIMEOUT
        flow_control = isotp.encode_flow_control(ext_address=ecu.ext_address, padding=policy.PADDING_BYTE)
        try:
            self._transmit(ecu.request_id, flow_control, purpose=pending.request.purpose.value, kind="flow_control")
        except SafetyError as exc:  # already audited where it was refused
            self._trip("flow_control_refused", str(exc))
            return
        pending._flow_control_sent = True

    def _handle_consecutive_frame(self, pending: Exchange, can_id: int, parsed: isotp.IsoTpFrame, now: float) -> None:
        if pending._rx_id != can_id:
            return
        if parsed.sequence != pending._rx_sequence:
            self._auditor.event("isotp_sequence_error", can_id=_hex_id(can_id), expected=pending._rx_sequence)
            self._finish(pending, ExchangeState.ABORTED, now)
            return
        pending._rx_data.extend(parsed.payload)
        pending._rx_sequence = (pending._rx_sequence + 1) & 0x0F
        pending._deadline = now + CONSECUTIVE_FRAME_TIMEOUT
        if len(pending._rx_data) >= pending._rx_length:
            payload = bytes(pending._rx_data[: pending._rx_length])
            pending._rx_id = None
            pending._flow_control_sent = False
            self._handle_payload(pending, can_id, payload, now)

    def _handle_payload(self, pending: Exchange, can_id: int, payload: bytes, now: float) -> None:
        request = pending.request
        if payload[0] == NEGATIVE_RESPONSE:
            if len(payload) < 3 or payload[1] != request.service:
                self._trip("unexpected_response", f"negative response {payload.hex(' ')} to service 0x{request.service:02X}")
                return
            nrc = payload[2]
            if nrc == NRC_RESPONSE_PENDING:
                pending._pending_extensions += 1
                if pending._pending_extensions > MAX_RESPONSE_PENDING:
                    self._finish_timeout(pending, now)
                else:
                    pending._deadline = now + P2_STAR_TIMEOUT
                return
            if nrc == NRC_BUSY_REPEAT_REQUEST:
                self._limiter.backoff(now)
            self._auditor.event(
                "negative_response", can_id=_hex_id(can_id), service=f"0x{request.service:02X}", nrc=f"0x{nrc:02X}"
            )
            self._finish(pending, ExchangeState.NEGATIVE, now, nrc=nrc)
            self._nrc_monitor.record_negative(request.key, nrc, request.purpose, now)
            return
        if payload[0] != request.service + POSITIVE_RESPONSE_OFFSET:
            self._trip("unexpected_response", f"response 0x{payload[0]:02X} to service 0x{request.service:02X}")
            return
        pending._responses.append((can_id, payload))
        self._nrc_monitor.record_positive(request.key, _target_key(request))
        self._limiter.clear_backoff()
        self._feed_interlocks(payload, now)
        if pending.can_id != policy.FUNCTIONAL_REQUEST_ID:
            self._finish(pending, ExchangeState.DONE, now)

    def _feed_interlocks(self, payload: bytes, now: float) -> None:
        """Single-PID Mode 01 answers for speed, RPM, and voltage update the interlocks."""
        if len(payload) < 3 or payload[0] != 0x41:
            return
        pid, data = payload[1], payload[2:]
        if pid == 0x0D and len(data) == 1:
            self._interlocks.update_speed(data[0], now, source="poll")
        elif pid == 0x0C and len(data) == 2:
            self._interlocks.update_rpm(((data[0] << 8) | data[1]) / 4, now)
        elif pid == 0x42 and len(data) == 2:
            self._interlocks.update_voltage(((data[0] << 8) | data[1]) / 1000, now)

    # ---- finishing ----

    def _finish(self, exchange: Exchange, state: ExchangeState, now: float, *, nrc: int | None = None) -> None:
        exchange._state = state
        exchange._nrc = nrc
        self._pending = None
        self._last = exchange
        self._last_finished = now

    def _finish_timeout(self, exchange: Exchange, now: float) -> None:
        self._finish(exchange, ExchangeState.TIMEOUT, now)
        if exchange.can_id != policy.FUNCTIONAL_REQUEST_ID:
            self._nrc_monitor.record_timeout(_target_key(exchange.request))

    def _trip(self, cause: str, detail: str) -> None:
        self._auditor.event("gate_anomaly", cause=cause, detail=detail)
        KILL_SWITCH.trip(cause)
        self.abort()


freeze(__name__)
