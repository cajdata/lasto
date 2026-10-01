r"""Nothing in the process opens a serial port except the STN adapter link (finding P2).

On Windows the built-in open() or os.open() reaches a serial port without
pyserial: open("COM5", "r+b") or os.open(r"\\.\COM5", ...) opens the MX+ for
writing. Whoever held it could send the adapter any line, and a hex-only line
goes to the vehicle as a request, with any service.

So the first import of the safety core (the package imports this module)
installs a process-wide audit hook. Opening a serial device path by name, with
open(), os.open(), or _winapi.CreateFile, is refused and audited before the
device is touched. pyserial, which lasto.safety.stn_port uses, opens its port
with CreateFileW through ctypes, which raises no such event, so the adapter
link is unaffected. The structural tests ban serial device paths in the
source as well, and the test firewall refuses them on its own.
"""

from __future__ import annotations

import os
import re
import sys

from lasto.safety._frozen import freeze
from lasto.safety.audit import refuse
from lasto.safety.errors import SafetyViolation

# The audit events that open a file by name. The name is the first argument.
OPEN_EVENTS = frozenset({"open", "_winapi.CreateFile"})
# Windows serial device names: COM1 and up (COM¹ to COM³ are reserved too), and AUX, which is COM1. GLOBALROOT
# reaches every device, serial ports included.
_DEVICE_NAME = re.compile(r"(?i)com[0-9¹²³]+|aux|globalroot")


def is_serial_device_path(path: object) -> bool:
    """Whether opening `path` could reach a serial port: a COM or AUX name, or GLOBALROOT, anywhere in it.

    Windows sends a reserved name to the device whatever directory or extension comes with it (COM5.txt),
    and a device namespace path names the device directly.
    """
    if isinstance(path, os.PathLike):
        path = os.fspath(path)
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if not isinstance(path, str):
        return False  # a file descriptor: already open
    for part in path.replace("/", "\\").split("\\"):
        name = part.split(".")[0].split(":")[0].rstrip(" ")
        if _DEVICE_NAME.fullmatch(name):
            return True
    return False


def refuse_serial_device_opens(event: str, args: tuple[object, ...]) -> None:
    """The audit hook: refuse (audited) opening a serial device by name anywhere in the process."""
    if event in OPEN_EVENTS and is_serial_device_path(args[0]):
        refuse(
            SafetyViolation("serial_port_outside_stn_port", f"{args[0]!r}; only lasto.safety.stn_port opens a serial port"),
            transport="stn",
            request=f"{event} {args[0]!r}",
        )


# Guard v2 (Phase 3) lets a process write only to its data folder, so from here on Python writes no .pyc files.
# Install with `uv sync --locked --compile-bytecode`, so startup stays fast without them.
sys.dont_write_bytecode = True
sys.addaudithook(refuse_serial_device_opens)
freeze(__name__)
