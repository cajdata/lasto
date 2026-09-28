"""The polled (normal-mode) PCAN channel, and the one CAN_Write binding in lasto.

open_active opens a channel in normal mode and returns two things: an
ActiveChannel, which can only read (and switch back to listen-only after a
kill), and a Writer, the only object in lasto that holds CAN_Write. Only
lasto.safety.session calls open_active, and it gives the Writer to the gate
and to nothing else.

The Writer doesn't trust its caller. For every frame it runs the policy's
full stateless check on the exact bytes, confirms the frame is the kind the
caller says it is, checks the kill switch, and writes the audit record before
the frame goes out. So even a direct call can only send what the policy
allows. The gate adds the checks that need state: flow control only for a
first frame that is waiting for it, the interlocks, and the rate limits.
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable, Collection
from typing import Any

from lasto.safety import pcan_constants as pc
from lasto.safety import policy
from lasto.safety.audit import Auditor, refuse
from lasto.safety.errors import InterfaceError, SafetyViolation
from lasto.safety.killswitch import KillSwitch
from lasto.safety.pcan_dll import (
    PcanChannel,
    ReadOnlyPcan,
    bind_readonly,
    check_available,
    check_driver,
    load_library,
)


class ActiveChannel(PcanChannel):
    """A PCAN channel in normal mode. It reads; it has no way to write."""

    def enter_listen_only(self) -> bool:
        """Switch to listen-only after a kill, and report whether it read back as on."""
        status = self._pcan.set_value(self._handle, pc.PCAN_LISTEN_ONLY, pc.PCAN_PARAMETER_ON)
        return status == pc.PCAN_ERROR_OK and self.listen_only()


class Writer:
    """The one function that puts a frame on the bus. Called as writer(can_id, data, purpose=..., kind=...)."""

    __slots__ = ("_auditor", "_broadcast_ids", "_can_write", "_channel_name", "_error_text", "_handle", "_killswitch")

    def __init__(
        self,
        can_write: Callable[..., int],
        handle: int,
        channel_name: str,
        error_text: Callable[[int], str],
        killswitch: KillSwitch,
        auditor: Auditor,
        broadcast_ids: frozenset[int],
    ) -> None:
        self._can_write = can_write
        self._handle = handle
        self._channel_name = channel_name
        self._error_text = error_text
        self._killswitch = killswitch
        self._auditor = auditor
        self._broadcast_ids = broadcast_ids

    def __call__(self, can_id: int, data: bytes, *, purpose: str, kind: str) -> None:
        text = policy.frame_text(can_id, data)
        checked = policy.check_frame(can_id, data, broadcast_ids=self._broadcast_ids)
        if kind != checked:
            refuse(
                SafetyViolation("frame_kind_mismatch", f"a {checked} frame sent as {kind!r}"),
                transport="pcan",
                request=text,
            )
        self._killswitch.check(request=text)
        self._auditor.transmit(transport="pcan", can_id=can_id, data=data, purpose=purpose, kind=kind)
        message = pc.TPCANMsg()
        message.ID = can_id
        message.MSGTYPE = pc.PCAN_MESSAGE_STANDARD
        message.LEN = len(data)
        for index, byte in enumerate(data):
            message.DATA[index] = byte
        status = int(self._can_write(ctypes.c_uint16(self._handle), ctypes.pointer(message))) & 0xFFFFFFFF
        if status != pc.PCAN_ERROR_OK:
            raise InterfaceError(f"CAN_Write failed on {self._channel_name}: {self._error_text(status)}")


def _clear_listen_only(set_value: Callable[..., int], handle: int) -> int:
    """The one CAN_SetValue that turns listen-only off: before a polled channel is initialized, and only here."""
    buffer = ctypes.c_uint32(pc.PCAN_PARAMETER_OFF)
    status = set_value(
        ctypes.c_uint16(handle),
        ctypes.c_uint8(pc.PCAN_LISTEN_ONLY),
        ctypes.pointer(buffer),
        ctypes.c_uint32(ctypes.sizeof(buffer)),
    )
    return int(status) & 0xFFFFFFFF


def open_active(
    channel_name: str,
    *,
    killswitch: KillSwitch,
    auditor: Auditor,
    library: object | None = None,
    broadcast_ids: Collection[int] = frozenset(),
) -> tuple[ActiveChannel, Writer]:
    """Open a channel in normal mode, from the real DLL or a stand-in. Only the polled session calls this."""
    handle = pc.channel_handle(channel_name)
    source: Any = load_library() if library is None else library
    functions = bind_readonly(source)
    pcan = ReadOnlyPcan(functions)
    api_version = check_driver(pcan)
    check_available(pcan, handle, channel_name)
    status = _clear_listen_only(functions["CAN_SetValue"], handle)
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
        writer = Writer(
            source.CAN_Write, handle, channel_name, pcan.error_text, killswitch, auditor, frozenset(broadcast_ids)
        )
    except BaseException:
        channel.close()
        raise
    return channel, writer
