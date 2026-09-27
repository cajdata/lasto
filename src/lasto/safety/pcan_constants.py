"""PCAN-Basic constants and structures, with no function bindings.

Values match PEAK's PCANBasic.py and PCANBasic.h. Nothing here touches the DLL,
so the simulator and the rest of lasto may import this module.
"""

from __future__ import annotations

import ctypes
import re

from lasto.safety.audit import refuse

# Channel handles. Only PCAN-USB, the hardware this project uses.
PCAN_NONEBUS = 0x00
USB_CHANNELS = {n: (0x50 + n if n <= 8 else 0x500 + n) for n in range(1, 17)}
_CHANNEL_NAME = re.compile(r"PCAN_USBBUS([1-9]|1[0-6])")

# Status codes. These are bit flags; test them with masks.
PCAN_ERROR_OK = 0x00000
PCAN_ERROR_XMTFULL = 0x00001
PCAN_ERROR_OVERRUN = 0x00002
PCAN_ERROR_BUSLIGHT = 0x00004
PCAN_ERROR_BUSHEAVY = 0x00008
PCAN_ERROR_BUSWARNING = PCAN_ERROR_BUSHEAVY
PCAN_ERROR_BUSOFF = 0x00010
PCAN_ERROR_QRCVEMPTY = 0x00020
PCAN_ERROR_QOVERRUN = 0x00040
PCAN_ERROR_QXMTFULL = 0x00080
PCAN_ERROR_NODRIVER = 0x00200
PCAN_ERROR_HWINUSE = 0x00400
PCAN_ERROR_ILLHW = 0x01400
PCAN_ERROR_ILLHANDLE = 0x01C00
PCAN_ERROR_RESOURCE = 0x02000
PCAN_ERROR_ILLPARAMTYPE = 0x04000
PCAN_ERROR_ILLPARAMVAL = 0x08000
PCAN_ERROR_UNKNOWN = 0x10000
PCAN_ERROR_ILLDATA = 0x20000
PCAN_ERROR_BUSPASSIVE = 0x40000
PCAN_ERROR_ILLMODE = 0x80000
PCAN_ERROR_CAUTION = 0x2000000
PCAN_ERROR_INITIALIZE = 0x4000000
PCAN_ERROR_ILLOPERATION = 0x8000000

# A read with any of these bits set still carries a valid (status) message.
BUS_STATUS_BITS = PCAN_ERROR_BUSLIGHT | PCAN_ERROR_BUSHEAVY | PCAN_ERROR_BUSPASSIVE | PCAN_ERROR_BUSOFF
# Bus states that stop transmission (rule 7). Warning levels are only recorded.
BUS_FAULT_BITS = PCAN_ERROR_BUSPASSIVE | PCAN_ERROR_BUSOFF
# Frames were lost because they weren't read in time.
OVERRUN_BITS = PCAN_ERROR_OVERRUN | PCAN_ERROR_QOVERRUN
# The adapter, driver, or channel is gone or unusable.
INTERFACE_FAILURE_BITS = (
    PCAN_ERROR_NODRIVER
    | PCAN_ERROR_ILLHANDLE
    | PCAN_ERROR_RESOURCE
    | PCAN_ERROR_UNKNOWN
    | PCAN_ERROR_ILLMODE
    | PCAN_ERROR_INITIALIZE
    | PCAN_ERROR_ILLOPERATION
)

# Parameters
PCAN_API_VERSION = 0x05
PCAN_CHANNEL_VERSION = 0x06
PCAN_LISTEN_ONLY = 0x08
PCAN_CHANNEL_CONDITION = 0x0D
PCAN_HARDWARE_NAME = 0x0E
PCAN_ALLOW_STATUS_FRAMES = 0x1E
PCAN_ALLOW_ERROR_FRAMES = 0x20
PCAN_PARAMETER_OFF = 0x00
PCAN_PARAMETER_ON = 0x01
PCAN_CHANNEL_UNAVAILABLE = 0x00
PCAN_CHANNEL_AVAILABLE = 0x01
PCAN_CHANNEL_OCCUPIED = 0x02
PCAN_CHANNEL_PCANVIEW = 0x03

# Message types
PCAN_MESSAGE_STANDARD = 0x00
PCAN_MESSAGE_RTR = 0x01
PCAN_MESSAGE_EXTENDED = 0x02
PCAN_MESSAGE_ERRFRAME = 0x40
PCAN_MESSAGE_STATUS = 0x80

PCAN_BAUD_500K = 0x001C
LANGUAGE_ENGLISH = 0x09

# PCAN-Basic 4.7.0 fixed GetValue on x64; 5.0.0 mishandles status messages.
MIN_API_VERSION = (4, 7, 0)
BROKEN_API_VERSIONS = frozenset({(5, 0, 0)})


class TPCANMsg(ctypes.Structure):
    _fields_ = [
        ("ID", ctypes.c_uint32),
        ("MSGTYPE", ctypes.c_uint8),
        ("LEN", ctypes.c_uint8),
        ("DATA", ctypes.c_uint8 * 8),
    ]


class TPCANTimestamp(ctypes.Structure):
    _fields_ = [
        ("millis", ctypes.c_uint32),
        ("millis_overflow", ctypes.c_uint16),
        ("micros", ctypes.c_uint16),
    ]


def channel_handle(name: str) -> int:
    """Handle for a channel name like PCAN_USBBUS1. Only PCAN-USB channels are accepted."""
    match = _CHANNEL_NAME.fullmatch(name) if isinstance(name, str) else None
    if match is None:
        refuse(ValueError(f"not a PCAN-USB channel name: {name!r}"), transport="pcan", reason="bad_channel_name")
    return USB_CHANNELS[int(match.group(1))]


def timestamp_us(stamp: TPCANTimestamp) -> int:
    """Microseconds since Windows started, per PCANBasic.h."""
    return stamp.micros + 1000 * stamp.millis + 0x100000000 * 1000 * stamp.millis_overflow


def parse_api_version(text: str) -> tuple[int, int, int] | None:
    parts = re.findall(r"\d+", text)
    if len(parts) < 3:
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def api_version_supported(version: tuple[int, int, int] | None) -> bool:
    return version is not None and version >= MIN_API_VERSION and version not in BROKEN_API_VERSIONS
