"""Read-only ctypes binding for PEAK's PCANBasic.dll.

The binding looks up only the functions in READONLY_FUNCTIONS. It never looks
up CAN_Write (or the FD and XL variants), CAN_Reset (can hard-reset the
controller), or CAN_FilterMessages (resets the controller), and it keeps no
reference to the DLL after binding. Code holding a ReadOnlyPcan or a
PcanChannel therefore has no path to a transmit function. The transmit
binding is in pcan_active.py, which only a polled session uses.
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable, Iterable, Mapping

from lasto.safety import pcan_constants as pc
from lasto.safety.audit import refuse
from lasto.safety.errors import InterfaceError, SafetyViolation
from lasto.safety.frames import CanFrame, ErrorFrame, ReadError, Received, StatusMessage

READONLY_FUNCTIONS = (
    "CAN_Initialize",
    "CAN_Uninitialize",
    "CAN_GetValue",
    "CAN_SetValue",
    "CAN_GetStatus",
    "CAN_Read",
    "CAN_GetErrorText",
)

# Most frames drained in one call, so a flood can't starve the status checks.
MAX_DRAIN = 4096

POINTER_BYTES = ctypes.sizeof(ctypes.c_void_p)


def dll_path() -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "PCANBasic.dll")


def load_library() -> object:
    """Load the real 64-bit PCANBasic.dll by absolute path. Only live hardware sessions get here."""
    if POINTER_BYTES != 8:
        raise InterfaceError("lasto needs 64-bit Python to load the 64-bit PCANBasic.dll")
    path = dll_path()
    try:
        return ctypes.WinDLL(path)
    except OSError as exc:
        raise InterfaceError(f"could not load {path}: {exc}") from exc


def bind(library: object, names: Iterable[str]) -> dict[str, Callable[..., int]]:
    return {name: getattr(library, name) for name in names}


def load_readonly(library: object | None = None) -> ReadOnlyPcan:
    """Bind the read-only subset, from the real DLL or from a stand-in such as the simulator."""
    source = load_library() if library is None else library
    return ReadOnlyPcan(bind(source, READONLY_FUNCTIONS))


class ReadOnlyPcan:
    """PCAN-Basic calls that can't transmit. CAN_SetValue only accepts an explicit list of settings."""

    SETVALUE_ALLOWED: frozenset[tuple[int, int]] = frozenset(
        {
            (pc.PCAN_LISTEN_ONLY, pc.PCAN_PARAMETER_ON),
            (pc.PCAN_ALLOW_ERROR_FRAMES, pc.PCAN_PARAMETER_ON),
            (pc.PCAN_ALLOW_STATUS_FRAMES, pc.PCAN_PARAMETER_ON),
        }
    )

    def __init__(self, functions: Mapping[str, Callable[..., int]]) -> None:
        self._functions = dict(functions)

    def _call(self, name: str, *args: object) -> int:
        return int(self._functions[name](*args)) & 0xFFFFFFFF

    def initialize(self, handle: int, baud: int) -> int:
        return self._call(
            "CAN_Initialize",
            ctypes.c_uint16(handle),
            ctypes.c_uint16(baud),
            ctypes.c_uint8(0),
            ctypes.c_uint32(0),
            ctypes.c_uint16(0),
        )

    def uninitialize(self, handle: int) -> int:
        return self._call("CAN_Uninitialize", ctypes.c_uint16(handle))

    def get_status(self, handle: int) -> int:
        return self._call("CAN_GetStatus", ctypes.c_uint16(handle))

    def get_u32(self, handle: int, parameter: int) -> tuple[int, int]:
        value = ctypes.c_uint32(0)
        status = self._call(
            "CAN_GetValue",
            ctypes.c_uint16(handle),
            ctypes.c_uint8(parameter),
            ctypes.pointer(value),
            ctypes.c_uint32(ctypes.sizeof(value)),
        )
        return status, value.value

    def get_text(self, handle: int, parameter: int) -> tuple[int, str]:
        buffer = ctypes.create_string_buffer(256)
        status = self._call(
            "CAN_GetValue",
            ctypes.c_uint16(handle),
            ctypes.c_uint8(parameter),
            ctypes.pointer(buffer),
            ctypes.c_uint32(ctypes.sizeof(buffer)),
        )
        return status, buffer.value.decode("ascii", "replace")

    def set_value(self, handle: int, parameter: int, value: int) -> int:
        if (parameter, value) not in self.SETVALUE_ALLOWED:
            refuse(
                SafetyViolation("pcan_setting_not_allowed", f"parameter 0x{parameter:02X} = {value}"),
                transport="pcan",
                request=f"CAN_SetValue 0x{parameter:02X}={value}",
            )
        buffer = ctypes.c_uint32(value)
        return self._call(
            "CAN_SetValue",
            ctypes.c_uint16(handle),
            ctypes.c_uint8(parameter),
            ctypes.pointer(buffer),
            ctypes.c_uint32(ctypes.sizeof(buffer)),
        )

    def read(self, handle: int) -> tuple[int, pc.TPCANMsg, pc.TPCANTimestamp]:
        message = pc.TPCANMsg()
        stamp = pc.TPCANTimestamp()
        status = self._call("CAN_Read", ctypes.c_uint16(handle), ctypes.pointer(message), ctypes.pointer(stamp))
        return status, message, stamp

    def error_text(self, status: int) -> str:
        buffer = ctypes.create_string_buffer(256)
        result = self._call(
            "CAN_GetErrorText", ctypes.c_uint32(status), ctypes.c_uint16(pc.LANGUAGE_ENGLISH), ctypes.pointer(buffer)
        )
        text = buffer.value.decode("ascii", "replace") if result == pc.PCAN_ERROR_OK else ""
        return f"0x{status:X} {text}".strip()


def convert(message: pc.TPCANMsg, stamp: pc.TPCANTimestamp) -> Received:
    when = pc.timestamp_us(stamp)
    kind = message.MSGTYPE
    if kind & pc.PCAN_MESSAGE_STATUS:
        return StatusMessage(int.from_bytes(bytes(message.DATA[:4]), "big"), when)
    data = bytes(message.DATA[: min(int(message.LEN), 8)])
    if kind & pc.PCAN_MESSAGE_ERRFRAME:
        return ErrorFrame(int(message.ID), data, when)
    return CanFrame(
        int(message.ID),
        data,
        when,
        extended=bool(kind & pc.PCAN_MESSAGE_EXTENDED),
        rtr=bool(kind & pc.PCAN_MESSAGE_RTR),
    )


def check_driver(pcan: ReadOnlyPcan) -> str:
    """Refuse PCAN-Basic versions older than 4.7.0, or 5.0.0. Returns the version text."""
    status, text = pcan.get_text(pc.PCAN_NONEBUS, pc.PCAN_API_VERSION)
    if status != pc.PCAN_ERROR_OK:
        raise InterfaceError(f"could not read the PCAN-Basic version: {pcan.error_text(status)}")
    if not pc.api_version_supported(pc.parse_api_version(text)):
        raise InterfaceError(f"PCAN-Basic {text!r} isn't supported; install 4.7.0 or later, but not 5.0.0")
    return text


def check_available(pcan: ReadOnlyPcan, handle: int, name: str) -> None:
    """Refuse a channel another program holds: its controller may already be running in normal mode."""
    status, condition = pcan.get_u32(handle, pc.PCAN_CHANNEL_CONDITION)
    if status != pc.PCAN_ERROR_OK:
        raise InterfaceError(f"could not check {name}: {pcan.error_text(status)}")
    if condition != pc.PCAN_CHANNEL_AVAILABLE:
        raise InterfaceError(
            f"{name} isn't available (condition {condition}); make sure the adapter is plugged in "
            "and close PCAN-View or any other program using it"
        )


class PcanChannel:
    """An initialized PCAN channel. Read operations only."""

    def __init__(self, pcan: ReadOnlyPcan, handle: int, name: str, api_version: str) -> None:
        self._pcan = pcan
        self._handle = handle
        self._name = name
        self._api_version = api_version
        self._open = True

    @property
    def name(self) -> str:
        return self._name

    def read_one(self) -> Received | None:
        status, message, stamp = self._pcan.read(self._handle)
        if status == pc.PCAN_ERROR_QRCVEMPTY:
            return None
        if status != pc.PCAN_ERROR_OK and not status & pc.BUS_STATUS_BITS:
            return ReadError(status)
        return convert(message, stamp)

    def drain(self, limit: int = MAX_DRAIN) -> list[Received]:
        items: list[Received] = []
        for _ in range(limit):
            item = self.read_one()
            if item is None:
                break
            items.append(item)
            if isinstance(item, ReadError):
                break
        return items

    def status(self) -> int:
        return self._pcan.get_status(self._handle)

    def listen_only(self) -> bool:
        status, value = self._pcan.get_u32(self._handle, pc.PCAN_LISTEN_ONLY)
        return status == pc.PCAN_ERROR_OK and value == pc.PCAN_PARAMETER_ON

    def enable_reporting(self) -> None:
        """Ask the driver to deliver error frames and bus-state messages, which the kill switch watches."""
        for parameter in (pc.PCAN_ALLOW_ERROR_FRAMES, pc.PCAN_ALLOW_STATUS_FRAMES):
            status = self._pcan.set_value(self._handle, parameter, pc.PCAN_PARAMETER_ON)
            if status != pc.PCAN_ERROR_OK:
                raise InterfaceError(
                    f"could not enable error and status reporting on {self._name}: {self._pcan.error_text(status)}"
                )

    def describe(self) -> dict[str, str]:
        info = {"channel": self._name, "api_version": self._api_version}
        for key, parameter in (("hardware", pc.PCAN_HARDWARE_NAME), ("channel_version", pc.PCAN_CHANNEL_VERSION)):
            status, text = self._pcan.get_text(self._handle, parameter)
            info[key] = text if status == pc.PCAN_ERROR_OK else "unknown"
        return info

    def close(self) -> None:
        if self._open:
            self._open = False
            self._pcan.uninitialize(self._handle)
