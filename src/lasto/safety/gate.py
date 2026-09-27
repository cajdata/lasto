"""The transmit gate: the only path from a typed request to the CAN bus.

Every frame lasto sends in polled mode goes through Gate._transmit. It
re-checks the exact bytes against the policy immediately before writing,
records them in the audit log first, and then calls the channel's write.
Requests arrive as typed Request objects from lasto.safety.requests; nothing
here accepts raw bytes or CAN IDs from callers.

The gate also watches every received frame. A frame on a request ID that
lasto didn't send (another tester, or broadcast traffic), a diagnostic
response nobody asked for, a malformed response, or a flow control frame
from an ECU all trip the kill switch.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from lasto.safety import ecus, isotp, policy
from lasto.safety.audit import Auditor
from lasto.safety.clock import Clock
from lasto.safety.errors import InterfaceError, SafetyError, SafetyViolation
from lasto.safety.frames import CanFrame, Received
from lasto.safety.interlocks import Interlocks
from lasto.safety.killswitch import KillSwitch, NrcMonitor
from lasto.safety.ratelimit import RateLimiter
from lasto.safety.requests import Purpose, Request

# ISO 15765-4 timing, with some margin for a Windows laptop.
P2_TIMEOUT = 0.15
P2_STAR_TIMEOUT = 5.0
CONSECUTIVE_FRAME_TIMEOUT = 0.25
MAX_RESPONSE_PENDING = 10
# The gate never waits longer than this for a rate-limit slot; the caller retries later.
MAX_RATE_WAIT = 1.0
# A slow answer to the previous request isn't mistaken for another tester.
LATE_RESPONSE_GRACE = 1.0

NEGATIVE_RESPONSE = 0x7F
NRC_BUSY_REPEAT_REQUEST = 0x21
NRC_RESPONSE_PENDING = 0x78
POSITIVE_RESPONSE_OFFSET = 0x40


class Link(Protocol):
    def write(self, can_id: int, data: bytes) -> None:
        """Put one 8-byte standard frame on the bus, or raise InterfaceError."""


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


def _describe(request: object) -> str:
    if isinstance(request, Request):
        target = "functional" if request.target is None else request.target.name
        return f"{target} {request.payload.hex(' ').upper()} ({request.purpose.value})"
    return type(request).__name__


def _hex_id(can_id: object) -> str:
    return f"0x{can_id:03X}" if isinstance(can_id, int) else repr(can_id)


def _target_key(request: Request) -> str:
    return "functional" if request.target is None else request.target.name


class Gate:
    def __init__(
        self,
        link: Link,
        auditor: Auditor,
        clock: Clock,
        killswitch: KillSwitch,
        interlocks: Interlocks,
        limiter: RateLimiter,
        nrc_monitor: NrcMonitor,
        *,
        profile: Iterable[Request],
        broadcast_ids: Iterable[int] = (),
    ) -> None:
        keys = set()
        for request in profile:
            if not isinstance(request, Request) or request.purpose is not Purpose.LOGGING:
                raise ValueError("a logging profile holds logging requests made with lasto.safety.requests")
            keys.add(request.key)
        self._profile = frozenset(keys)
        self._link = link
        self._auditor = auditor
        self._clock = clock
        self._killswitch = killswitch
        self._interlocks = interlocks
        self._limiter = limiter
        self._nrc_monitor = nrc_monitor
        self._broadcast_ids = frozenset(broadcast_ids)
        self._request_ids = frozenset({policy.FUNCTIONAL_REQUEST_ID, *(ecu.request_id for ecu in ecus.APPROVED_ECUS)})
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
            try:
                return self._submit(request)
            except SafetyError as exc:
                self._auditor.rejected(
                    transport="pcan", reason=getattr(exc, "reason", "refused"), detail=str(exc), request=_describe(request)
                )
                raise

    def _submit(self, request: Request) -> Exchange:
        self._killswitch.check()
        if not self._armed:
            raise SafetyViolation("gate_not_armed", "the polled session hasn't finished its listen window")
        if not isinstance(request, Request):
            raise SafetyViolation("not_a_typed_request", type(request).__name__)
        if self._pending is not None:
            raise SafetyViolation("request_outstanding", "wait for the previous request to finish")
        target = request.target
        if target is not None and not ecus.is_approved(target):
            raise SafetyViolation("ecu_not_approved", target.name)
        now = self._clock.monotonic()
        self._interlocks.check(request, now, self._profile)
        can_id = policy.FUNCTIONAL_REQUEST_ID if target is None else target.request_id
        data = isotp.encode_single_frame(
            request.payload, ext_address=None if target is None else target.ext_address, padding=policy.PADDING_BYTE
        )
        self._check_request_frame(can_id, data)
        wait = self._limiter.delay(request.purpose, now)
        if wait > MAX_RATE_WAIT:
            raise SafetyViolation("rate_limited", f"next slot in {wait:.3f} s")
        if wait > 0:
            self._clock.sleep(wait)
            now = self._clock.monotonic()
        self._transmit(can_id, data, purpose=request.purpose.value, kind="request")
        self._limiter.commit(request.purpose, now)
        exchange = Exchange(request, can_id, sent_at=now, deadline=now + P2_TIMEOUT)
        self._pending = exchange
        return exchange

    def _check_request_frame(self, can_id: int, data: bytes) -> None:
        if len(data) != isotp.FRAME_BYTES:
            raise SafetyViolation("frame_length", f"{len(data)} bytes")
        if can_id not in self._request_ids:
            raise SafetyViolation("can_id_not_allowlisted", _hex_id(can_id))
        if can_id in self._broadcast_ids:
            raise SafetyViolation("can_id_carries_broadcast", _hex_id(can_id))
        ecu = ecus.by_request_id(can_id)
        frame = isotp.parse(data, ext_address=None if ecu is None else ecu.ext_address)
        if frame.kind is not isotp.FrameKind.SINGLE:
            raise SafetyViolation("not_single_frame", frame.kind.value)
        service = frame.payload[0]
        policy.check_service(service, functional=ecu is None)
        policy.check_sensitive(service, sensitive=ecu is not None and ecu.kind in ecus.SENSITIVE_KINDS)

    def _check_flow_control_frame(self, can_id: int, data: bytes) -> None:
        pending = self._pending
        if pending is None or pending.rx_id is None or pending.flow_control_sent:
            raise SafetyViolation("flow_control_unsolicited", "no first frame is waiting for flow control")
        ecu = ecus.by_response_id(pending.rx_id)
        if ecu is None or can_id != ecu.request_id:
            raise SafetyViolation("flow_control_wrong_id", _hex_id(can_id))
        if can_id in self._broadcast_ids:
            raise SafetyViolation("can_id_carries_broadcast", _hex_id(can_id))
        if data != isotp.encode_flow_control(ext_address=ecu.ext_address, padding=policy.PADDING_BYTE):
            raise SafetyViolation("flow_control_malformed", bytes(data).hex(" "))

    def _transmit(self, can_id: int, data: bytes, *, purpose: str, kind: str) -> None:
        """The last check before the wire, on the exact bytes. Every frame lasto sends passes here."""
        if kind == "request":
            self._check_request_frame(can_id, data)
        elif kind == "flow_control":
            self._check_flow_control_frame(can_id, data)
        else:
            raise SafetyViolation("unknown_frame_kind", repr(kind))
        self._killswitch.check()
        self._auditor.transmit(transport="pcan", can_id=can_id, data=data, purpose=purpose, kind=kind)
        try:
            self._link.write(can_id, data)
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
            elif not self._is_late(can_id, now):
                self._trip("unexpected_response", f"diagnostic response on {_hex_id(can_id)} with no matching request")

    def poll(self) -> None:
        """Finish an exchange whose deadline has passed."""
        with self._lock:
            pending = self._pending
            now = self._clock.monotonic()
            if pending is None or now < pending.deadline:
                return
            if pending.can_id == policy.FUNCTIONAL_REQUEST_ID and pending.responses and pending.rx_id is None:
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
        except SafetyViolation as exc:
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
        if pending.rx_id is not None:
            return  # already receiving from another responder; this one gets no flow control
        ecu = ecus.by_response_id(can_id)
        if ecu is None:
            self._auditor.event(
                "response_abandoned", can_id=_hex_id(can_id), detail="multi-frame response from an unapproved ECU"
            )
            return
        pending.rx_id = can_id
        pending.rx_length = parsed.length
        pending.rx_data = bytearray(parsed.payload)
        pending.rx_sequence = 1
        pending.deadline = now + CONSECUTIVE_FRAME_TIMEOUT
        flow_control = isotp.encode_flow_control(ext_address=ecu.ext_address, padding=policy.PADDING_BYTE)
        try:
            self._transmit(ecu.request_id, flow_control, purpose=pending.request.purpose.value, kind="flow_control")
        except SafetyError as exc:
            self._auditor.rejected(
                transport="pcan", reason=getattr(exc, "reason", "refused"), detail=str(exc), request="flow control"
            )
            self._trip("flow_control_refused", str(exc))
            return
        pending.flow_control_sent = True

    def _handle_consecutive_frame(self, pending: Exchange, can_id: int, parsed: isotp.IsoTpFrame, now: float) -> None:
        if pending.rx_id != can_id:
            return
        if parsed.sequence != pending.rx_sequence:
            self._auditor.event("isotp_sequence_error", can_id=_hex_id(can_id), expected=pending.rx_sequence)
            self._finish(pending, ExchangeState.ABORTED, now)
            return
        pending.rx_data.extend(parsed.payload)
        pending.rx_sequence = (pending.rx_sequence + 1) & 0x0F
        pending.deadline = now + CONSECUTIVE_FRAME_TIMEOUT
        if len(pending.rx_data) >= pending.rx_length:
            payload = bytes(pending.rx_data[: pending.rx_length])
            pending.rx_id = None
            pending.flow_control_sent = False
            self._handle_payload(pending, can_id, payload, now)

    def _handle_payload(self, pending: Exchange, can_id: int, payload: bytes, now: float) -> None:
        request = pending.request
        if payload[0] == NEGATIVE_RESPONSE:
            if len(payload) < 3 or payload[1] != request.service:
                self._trip("unexpected_response", f"negative response {payload.hex(' ')} to service 0x{request.service:02X}")
                return
            nrc = payload[2]
            if nrc == NRC_RESPONSE_PENDING:
                pending.pending_extensions += 1
                if pending.pending_extensions > MAX_RESPONSE_PENDING:
                    self._finish_timeout(pending, now)
                else:
                    pending.deadline = now + P2_STAR_TIMEOUT
                return
            if nrc == NRC_BUSY_REPEAT_REQUEST:
                self._limiter.backoff(now)
            self._auditor.event("negative_response", can_id=_hex_id(can_id), service=f"0x{request.service:02X}", nrc=f"0x{nrc:02X}")
            self._finish(pending, ExchangeState.NEGATIVE, now, nrc=nrc)
            self._nrc_monitor.record_negative(request.key, nrc, request.purpose, now)
            return
        if payload[0] != request.service + POSITIVE_RESPONSE_OFFSET:
            self._trip("unexpected_response", f"response 0x{payload[0]:02X} to service 0x{request.service:02X}")
            return
        pending.responses.append((can_id, payload))
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
        exchange.state = state
        exchange.nrc = nrc
        self._pending = None
        self._last = exchange
        self._last_finished = now

    def _finish_timeout(self, exchange: Exchange, now: float) -> None:
        self._finish(exchange, ExchangeState.TIMEOUT, now)
        if exchange.can_id != policy.FUNCTIONAL_REQUEST_ID:
            self._nrc_monitor.record_timeout(_target_key(exchange.request))

    def _trip(self, cause: str, detail: str) -> None:
        self._auditor.event("gate_anomaly", cause=cause, detail=detail)
        self._killswitch.trip(cause)
        self.abort()
