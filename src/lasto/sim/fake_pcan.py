"""FakePcanDll: stands in for PCANBasic.dll underneath the safety core.

It keeps the PCAN-Basic behavior that matters for safety. Listen-only is
taken from the pre-initialization setting when a channel is initialized and
cleared by uninitialize. A channel someone else holds isn't AVAILABLE. A
written frame reaches the simulated bus only in normal mode. The simulator's
oracle judges every write, and every function the binding looks up is
recorded, so a test can prove the passive path never even looked up
CAN_Write.
"""

from __future__ import annotations

from collections import deque

from lasto.safety import pcan_constants as pc
from lasto.sim.bus import SimBus
from lasto.sim.oracle import frame_violations
from lasto.sim.violations import VIOLATIONS

OK = pc.PCAN_ERROR_OK


def _v(value: object) -> int:
    return value.value if hasattr(value, "value") else value  # type: ignore[attr-defined,return-value]


class FakeChannelState:
    def __init__(self, handle: int) -> None:
        self.handle = handle
        self.condition = pc.PCAN_CHANNEL_AVAILABLE
        self.initialized = False
        self.preinit_listen_only = pc.PCAN_PARAMETER_OFF
        self.listen_only = pc.PCAN_PARAMETER_OFF
        self.listen_only_at_initialize: int | None = None
        self.error_frames = False
        self.status_frames = False
        self.status = OK
        self.unplugged = False
        # (read status, message type, CAN ID, data, time in seconds)
        self.rx: deque[tuple[int, int, int, bytes, float]] = deque()

    def receive(self, time: float, can_id: int, data: bytes) -> None:
        if self.initialized:
            self.rx.append((OK, pc.PCAN_MESSAGE_STANDARD, can_id, data, time))


class FakePcanDll:
    def __init__(
        self, bus: SimBus | None = None, *, api_version: str = "4.7.0.11", hardware: str = "PCAN-USB (simulated)"
    ) -> None:
        self.bus = bus
        self.api_version = api_version
        self.hardware = hardware
        self.looked_up: list[str] = []
        self.calls: list[str] = []
        self.channels: dict[int, FakeChannelState] = {}
        self.writes: list[tuple[int, int, bytes]] = []
        # Fault injection
        self.fail_get: dict[int, int] = {}
        self.fail_set: dict[int, int] = {}
        self.initialize_status = OK
        self.write_status = OK
        self.error_text_status = OK
        self.readback_listen_only: int | None = None

    def __getattribute__(self, name: str) -> object:
        if name.startswith("CAN_"):
            object.__getattribute__(self, "looked_up").append(name)
        return object.__getattribute__(self, name)

    def channel(self, handle: int) -> FakeChannelState:
        return self.channels.setdefault(handle, FakeChannelState(handle))

    # ---- PCAN-Basic functions ----

    def CAN_Initialize(self, handle, baud, hw_type, io_port, interrupt) -> int:
        ch = self.channel(_v(handle))
        self.calls.append("CAN_Initialize")
        if self.initialize_status != OK:
            return self.initialize_status
        if ch.unplugged:
            return pc.PCAN_ERROR_ILLHW
        if ch.initialized:
            return pc.PCAN_ERROR_ILLOPERATION
        ch.initialized = True
        ch.listen_only = ch.preinit_listen_only
        ch.listen_only_at_initialize = ch.listen_only
        ch.condition = pc.PCAN_CHANNEL_OCCUPIED
        if self.bus is not None:
            self.bus.add_tap(ch, ch.receive)
        return OK

    def CAN_Uninitialize(self, handle) -> int:
        ch = self.channel(_v(handle))
        self.calls.append("CAN_Uninitialize")
        if not ch.initialized:
            return pc.PCAN_ERROR_INITIALIZE
        ch.initialized = False
        ch.listen_only = pc.PCAN_PARAMETER_OFF
        ch.preinit_listen_only = pc.PCAN_PARAMETER_OFF
        ch.condition = pc.PCAN_CHANNEL_AVAILABLE
        ch.rx.clear()
        if self.bus is not None:
            self.bus.remove_tap(ch)
        return OK

    def CAN_GetValue(self, handle, parameter, buffer, length) -> int:
        param = _v(parameter)
        if param in self.fail_get:
            return self.fail_get[param]
        texts = {
            pc.PCAN_API_VERSION: self.api_version,
            pc.PCAN_HARDWARE_NAME: self.hardware,
            pc.PCAN_CHANNEL_VERSION: "simulated channel",
        }
        if param in texts:
            buffer.contents.value = texts[param].encode("ascii")
            return OK
        ch = self.channel(_v(handle))
        if param == pc.PCAN_CHANNEL_CONDITION:
            value = pc.PCAN_CHANNEL_UNAVAILABLE if ch.unplugged else ch.condition
        elif ch.unplugged:
            return pc.PCAN_ERROR_ILLHW
        elif param == pc.PCAN_LISTEN_ONLY:
            value = ch.listen_only if ch.initialized else ch.preinit_listen_only
            if self.readback_listen_only is not None:
                value = self.readback_listen_only
        else:
            return pc.PCAN_ERROR_ILLPARAMTYPE
        buffer.contents.value = value
        return OK

    def CAN_SetValue(self, handle, parameter, buffer, length) -> int:
        param, value = _v(parameter), buffer.contents.value
        self.calls.append(f"CAN_SetValue 0x{param:02X}={value}")
        if param in self.fail_set:
            return self.fail_set[param]
        ch = self.channel(_v(handle))
        if param == pc.PCAN_LISTEN_ONLY:
            if ch.initialized:
                ch.listen_only = value
            else:
                ch.preinit_listen_only = value
            return OK
        if param in (pc.PCAN_ALLOW_ERROR_FRAMES, pc.PCAN_ALLOW_STATUS_FRAMES):
            if not ch.initialized:
                return pc.PCAN_ERROR_INITIALIZE
            if param == pc.PCAN_ALLOW_ERROR_FRAMES:
                ch.error_frames = bool(value)
            else:
                ch.status_frames = bool(value)
            return OK
        return pc.PCAN_ERROR_ILLPARAMTYPE

    def CAN_GetStatus(self, handle) -> int:
        ch = self.channel(_v(handle))
        if ch.unplugged:
            return pc.PCAN_ERROR_ILLHW
        if not ch.initialized:
            return pc.PCAN_ERROR_INITIALIZE
        return ch.status

    def CAN_Read(self, handle, message, stamp) -> int:
        ch = self.channel(_v(handle))
        if ch.unplugged:
            return pc.PCAN_ERROR_ILLHW
        if not ch.initialized:
            return pc.PCAN_ERROR_INITIALIZE
        if self.bus is not None:
            self.bus.advance()
        if not ch.rx:
            return pc.PCAN_ERROR_QRCVEMPTY
        status, msgtype, can_id, data, time = ch.rx.popleft()
        msg = message.contents
        msg.ID = can_id
        msg.MSGTYPE = msgtype
        msg.LEN = len(data)
        for index, byte in enumerate(data[:8]):
            msg.DATA[index] = byte
        total_ms, micros = divmod(round(time * 1_000_000), 1000)
        overflow, millis = divmod(total_ms, 1 << 32)
        ts = stamp.contents
        ts.millis, ts.millis_overflow, ts.micros = millis, overflow, micros
        return status

    def CAN_Write(self, handle, message) -> int:
        ch = self.channel(_v(handle))
        msg = message.contents
        can_id, data = msg.ID, bytes(msg.DATA[: msg.LEN])
        self.writes.append((ch.handle, can_id, data))
        problems = frame_violations(
            can_id,
            data,
            listen_only=not ch.initialized or ch.listen_only == pc.PCAN_PARAMETER_ON,
            broadcast_ids=self.bus.broadcast_ids if self.bus is not None else (),
            awaiting_flow_control=self.bus.awaiting_flow_control() if self.bus is not None else (),
        )
        for problem in problems:
            VIOLATIONS.record(problem)
        if not ch.initialized:
            return pc.PCAN_ERROR_INITIALIZE
        if ch.listen_only == pc.PCAN_PARAMETER_ON:
            return pc.PCAN_ERROR_ILLOPERATION
        if self.write_status != OK:
            return self.write_status
        if self.bus is not None:
            self.bus.transmit(can_id, data, ch)
        return OK

    def CAN_GetErrorText(self, status, language, buffer) -> int:
        if self.error_text_status != OK:
            return self.error_text_status
        buffer.contents.value = b"simulated PCAN error"
        return OK

    # ---- fault injection for tests ----

    def _time(self) -> float:
        return self.bus.clock.monotonic() if self.bus is not None else 0.0

    def inject_frame(self, handle: int, can_id: int, data: bytes, *, msgtype: int = pc.PCAN_MESSAGE_STANDARD) -> None:
        self.channel(handle).rx.append((OK, msgtype, can_id, data, self._time()))

    def inject_error_frame(self, handle: int, error_type: int = 1, data: bytes = b"\x01\x19\x08\x00") -> None:
        self.channel(handle).rx.append((OK, pc.PCAN_MESSAGE_ERRFRAME, error_type, data, self._time()))

    def inject_status(self, handle: int, status: int) -> None:
        self.channel(handle).rx.append((status, pc.PCAN_MESSAGE_STATUS, 0, status.to_bytes(4, "big"), self._time()))

    def inject_read_error(self, handle: int, status: int) -> None:
        self.channel(handle).rx.append((status, pc.PCAN_MESSAGE_STANDARD, 0, b"", self._time()))

    def unplug(self, handle: int) -> None:
        """The adapter disappears: reads and status report ILLHW, and the channel isn't available."""
        self.channel(handle).unplugged = True

    def plug_back(self, handle: int) -> None:
        """The adapter comes back. If the channel was left initialized, the driver resumes it on its own."""
        self.channel(handle).unplugged = False

    def driver_resumes(self, handle: int, *, listen_only: int, announce: bool = True) -> None:
        """After a replug, the driver resumes the still-initialized channel by itself.

        PEAK doesn't document whether listen-only survives that. `announce` queues the
        driver's "controller activated" status message (status 0).
        """
        ch = self.channel(handle)
        ch.unplugged = False
        ch.listen_only = listen_only
        if announce:
            ch.rx.append((OK, pc.PCAN_MESSAGE_STATUS, 0, bytes(4), self._time()))
