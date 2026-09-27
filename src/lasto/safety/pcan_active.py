"""Normal-mode PCAN channel for polled sessions. The only CAN_Write binding in lasto.

Only lasto.safety.session opens one of these, and only the gate calls
ActiveChannel.write, after checking every frame against the policy.
"""

from __future__ import annotations

import ctypes

from lasto.safety import pcan_constants as pc
from lasto.safety.audit import refuse
from lasto.safety.errors import InterfaceError, SafetyViolation
from lasto.safety.pcan_dll import (
    READONLY_FUNCTIONS,
    PcanChannel,
    ReadOnlyPcan,
    bind,
    check_available,
    check_driver,
    load_library,
)

TRANSMIT_FUNCTIONS = (*READONLY_FUNCTIONS, "CAN_Write")


class TransmitPcan(ReadOnlyPcan):
    """PCAN-Basic calls for a polled channel: the read-only set, plus writing standard frames."""

    # A polled channel is initialized with listen-only explicitly off.
    SETVALUE_ALLOWED = ReadOnlyPcan.SETVALUE_ALLOWED | {(pc.PCAN_LISTEN_ONLY, pc.PCAN_PARAMETER_OFF)}

    def write_standard(self, handle: int, can_id: int, data: bytes) -> int:
        message = pc.TPCANMsg()
        message.ID = can_id
        message.MSGTYPE = pc.PCAN_MESSAGE_STANDARD
        message.LEN = len(data)
        for index, byte in enumerate(data):
            message.DATA[index] = byte
        return self._call("CAN_Write", ctypes.c_uint16(handle), ctypes.pointer(message))


def load_transmit(library: object | None = None) -> TransmitPcan:
    source = load_library() if library is None else library
    return TransmitPcan(bind(source, TRANSMIT_FUNCTIONS))


class ActiveChannel(PcanChannel):
    """A PCAN channel in normal mode. The gate is its only writer."""

    def __init__(self, pcan: TransmitPcan, handle: int, name: str, api_version: str) -> None:
        super().__init__(pcan, handle, name, api_version)
        self._transmit = pcan

    def write(self, can_id: int, data: bytes) -> None:
        if not 0 <= can_id <= 0x7FF or len(data) != 8:
            refuse(
                SafetyViolation("frame_shape", "lasto only sends 8-byte frames with 11-bit IDs"),
                transport="pcan",
                request=f"0x{can_id:X} {bytes(data).hex(' ').upper()}",
            )
        status = self._transmit.write_standard(self._handle, can_id, data)
        if status != pc.PCAN_ERROR_OK:
            raise InterfaceError(f"CAN_Write failed on {self.name}: {self._transmit.error_text(status)}")

    def enter_listen_only(self) -> bool:
        """Switch to listen-only after a kill, and report whether it read back as on."""
        status = self._transmit.set_value(self._handle, pc.PCAN_LISTEN_ONLY, pc.PCAN_PARAMETER_ON)
        return status == pc.PCAN_ERROR_OK and self.listen_only()


def open_active(channel_name: str, *, pcan: TransmitPcan | None = None) -> ActiveChannel:
    handle = pc.channel_handle(channel_name)
    pcan = load_transmit() if pcan is None else pcan
    api_version = check_driver(pcan)
    check_available(pcan, handle, channel_name)
    status = pcan.set_value(handle, pc.PCAN_LISTEN_ONLY, pc.PCAN_PARAMETER_OFF)
    if status != pc.PCAN_ERROR_OK:
        raise InterfaceError(f"could not clear listen-only on {channel_name}: {pcan.error_text(status)}")
    status = pcan.initialize(handle, pc.PCAN_BAUD_500K)
    if status != pc.PCAN_ERROR_OK:
        raise InterfaceError(f"could not initialize {channel_name}: {pcan.error_text(status)}")
    channel = ActiveChannel(pcan, handle, channel_name, api_version)
    try:
        status, value = pcan.get_u32(handle, pc.PCAN_LISTEN_ONLY)
        if status != pc.PCAN_ERROR_OK or value != pc.PCAN_PARAMETER_OFF:
            raise InterfaceError(f"{channel_name} did not come up in normal mode")
        channel.enable_reporting()
    except BaseException:
        channel.close()
        raise
    return channel
