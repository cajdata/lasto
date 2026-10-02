r"""Guard v2: a lasto process writes only inside its data folder, and starts no other (finding P2, Phase 3).

On Windows the built-in open() or os.open() reaches a serial port without pyserial: open("COM5", "r+b")
opens the MX+ for writing, and whoever held it could send the adapter any line, which a hex-only line
turns into a vehicle request. The first guard refused paths that looked like serial devices, and three
path forms got past it. So this guard turns the check around, to a short allow list:

- The first import of the safety core (the package imports this module) installs a process-wide audit
  hook. Every write-capable open, by open(), os.open(), pathlib, os.truncate, or _winapi.CreateFile, must
  pass write_problem(): inside a folder this process registered, every name below it a plain file or
  folder name, no link or junction on the way, and an existing regular file or a new one in a real
  folder. Anything else is refused, and audited, before it opens. Every device path form fails the first
  check without being listed.
- A process registers one folder, its data folder, once, before it writes (allow_writes_in). Only a test
  run, with the hardware firewall installed, may add more: its temp folder and tool caches.
- An open counts as a read only if its flags or access bits are all read-only ones; anything else is a
  write. Reads stay open everywhere (imports read files), since a read-only handle can't send the
  adapter anything. The old name check stays as a backstop for reads that name a serial device.
- SQLite opens its files itself. Its connect event names the database, a path or a file: URI, which must
  pass the same check in any mode (SQLite may create it), so a reader registers its data folder too. Only
  an in-memory database opens no file. Loading an SQLite extension, native code, is refused. ATTACH and
  VACUUM INTO open a second file with no event at all, and the guard can't put an authorizer on a new
  connection (Python raises the connect/handle event before the connection is initialized), so lasto's own
  connections refuse both (lasto.storage.database) and the scanner bans that SQL in src/.
- A child process runs without this process's hook, so outside a test run (which starts them) the guard
  refuses every way Python starts one: subprocess.Popen, _winapi.CreateProcess, os.system, os.startfile,
  os.spawn* and os.exec*. A Bluetooth socket could reach the MX+ with no COM port at all, so one is refused
  everywhere, before it exists.
- pyserial opens the adapter's port with CreateFileW through ctypes, which raises no open event, so the
  STN link is unaffected. Where ctypes may be used is checked separately.

From here on, Python writes no .pyc files (the data folder is the only place this process may write).
"""

from __future__ import annotations

import nturl2path
import os
import re
import stat
import sys
import threading
from urllib import parse

from lasto.safety._frozen import SealedType, freeze
from lasto.safety.audit import refuse
from lasto.safety.errors import SafetyViolation
from lasto.safety.pcan_dll import hardware_firewall_installed

# The audit events that open a file by name. The name is the first argument.
OPEN_EVENTS = frozenset({"open", "_winapi.CreateFile"})
# Windows serial device names: COM1 and up (COM¹ to COM³ are reserved too), and AUX, which is COM1. GLOBALROOT
# reaches every device, serial ports included. Used only as the backstop for reads.
_DEVICE_NAME = re.compile(r"(?i)com[0-9¹²³]+|aux|globalroot")

# What a written path's names may not be. Windows reads a name's base (before the first dot, trailing spaces
# dropped) as a device if it's a reserved one, whatever directory or extension comes with it, and treats a colon
# as a stream, a trailing dot or space as nothing at all.
_RESERVED = re.compile(r"(?i)con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$")
_INVALID = re.compile(r'[<>:"|?*\x00-\x1f]|[. ]$')
_DRIVE_FOLDER = re.compile(r"[A-Za-z]:\\")
# The extended-length form of a drive path (\\?\ before it): the same file, which Windows takes literally.
_EXTENDED_DRIVE_PATH = re.compile(r"\\\\\?\\(?=[A-Za-z]:\\)")

# Flags and access bits that only read. Any other bit makes an open a write (the allow list, the other way round).
_READ_FLAGS = os.O_RDONLY | os.O_BINARY | os.O_TEXT | os.O_NOINHERIT | os.O_SEQUENTIAL | os.O_RANDOM
_GENERIC_READ = 0x80000000
_READ_ACCESS = _GENERIC_READ | 0x0001 | 0x0008 | 0x0080 | 0x00020000 | 0x00100000  # data, EA, attributes, control, sync
_OPEN_EXISTING = 3

OUTSIDE = "outside the folders this process may write"

# The audit events that start another process, which runs without this process's hook (Windows: no fork).
CHILD_PROCESS_EVENTS = frozenset(
    {"subprocess.Popen", "_winapi.CreateProcess", "os.system", "os.startfile", "os.startfile/2", "os.spawn", "os.exec"}
)
_AF_BLUETOOTH = 32  # socket.AF_BLUETOOTH on Windows


def is_serial_device_path(path: object) -> bool:
    """Whether opening `path` could reach a serial port: a COM or AUX name, or GLOBALROOT, anywhere in it.

    The backstop for reads. Writes are decided by write_problem(), which doesn't depend on this list.
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


def _as_text(target: object) -> str | None:
    if isinstance(target, os.PathLike):
        target = os.fspath(target)
    if isinstance(target, bytes):
        target = os.fsdecode(target)
    return target if isinstance(target, str) else None


def _same(a: str, b: str) -> bool:
    return os.path.normcase(a) == os.path.normcase(b)


def _plain_name_problem(name: str) -> str | None:
    base = name.split(".")[0].rstrip(" ")
    if not name or _RESERVED.fullmatch(base) or _INVALID.search(name):
        return f"{name!r} isn't a plain file or folder name"
    return None


def write_problem(target: object, roots: tuple[str, ...]) -> str | None:
    """Why writing to `target` isn't allowed, or None if it is.

    In order, all lexical until the last two: inside one of `roots`; every name below the root a plain name;
    no link or junction on the way; an existing regular file, or a new one in a real folder.
    """
    if isinstance(target, int):
        return None  # an open descriptor: its path was checked when it was opened
    text = _as_text(target)
    if text is None:
        return "not a path"
    extended = _EXTENDED_DRIVE_PATH.match(text)
    if extended is not None:
        # The same file without Win32's path rewriting, so it isn't normalized: a "." or ".." name is refused below.
        full = text[extended.end() :]
    else:
        full = os.path.abspath(text)
    root = next((root for root in roots if os.path.normcase(full).startswith(os.path.normcase(root) + "\\")), None)
    if root is None:
        return OUTSIDE
    for name in full[len(root) + 1 :].split("\\"):
        problem = _plain_name_problem(name)
        if problem is not None:
            return problem
    if not _same(os.path.realpath(full), full):
        return "a link or junction leads elsewhere"
    try:
        mode = os.stat(full).st_mode
    except FileNotFoundError:
        try:
            parent = os.stat(os.path.dirname(full)).st_mode
        except FileNotFoundError:
            return "its folder doesn't exist"
        return None if stat.S_ISDIR(parent) else "its folder isn't a folder"
    return None if stat.S_ISREG(mode) else "not a regular file"


def _folder_problem(full: str) -> str | None:
    if not _DRIVE_FOLDER.match(full):
        return "not a folder on a drive"
    for name in full[3:].split("\\"):  # a whole drive (C:\) has an empty name here, and isn't a data folder
        problem = _plain_name_problem(name)
        if problem is not None:
            return problem
    if not _same(os.path.realpath(full), full):
        return "a link or junction leads elsewhere"
    try:
        is_folder = stat.S_ISDIR(os.stat(full).st_mode)
    except FileNotFoundError:
        is_folder = False
    return None if is_folder else "not an existing folder"


class WriteGuard(metaclass=SealedType):
    """The folders this process may write in: its data folder, registered once (in a test run, also its own)."""

    __slots__ = ("_lock", "_roots")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._roots: tuple[str, ...] = ()

    @property
    def roots(self) -> tuple[str, ...]:
        return self._roots

    def allow(self, folder: object, *, more_than_one: bool) -> None:
        """Let this process write inside `folder`. Refused (audited) for anything but a real folder on a drive,
        and for a second folder unless `more_than_one`."""
        text = _as_text(folder)
        full = os.path.abspath(text) if text is not None else repr(folder)
        request = f"allow writes in {full!r}"
        problem = _folder_problem(full) if text is not None else "not a path"
        if problem is not None:
            refuse(SafetyViolation("write_folder_refused", f"{full!r}: {problem}"), transport="core", request=request)
        with self._lock:
            if any(_same(full, root) for root in self._roots):
                return
            if self._roots and not more_than_one:
                refuse(
                    SafetyViolation("write_folder_already_set", f"this process already writes in {self._roots[0]!r}"),
                    transport="core",
                    request=request,
                )
            self._roots = (*self._roots, full)


GUARD = WriteGuard()


def allow_writes_in(folder: object) -> None:
    """Let this process write inside its data folder. Called once, at startup, before the first write.

    A second, different folder is refused (audited), except in a test run, which also registers its own.
    """
    GUARD.allow(folder, more_than_one=hardware_firewall_installed())


def sqlite_problem(database: object, roots: tuple[str, ...]) -> str | None:
    """Why sqlite3.connect may not open `database`, or None if it may.

    SQLite opens its files itself, so the connect event is the only place to check one. Any mode counts,
    since SQLite may create the file: a path or a file: URI must pass write_problem(). Only an in-memory
    database opens no file. SQLite's own temporary file ("") isn't one lasto ever needs.
    """
    text = _as_text(database)
    if text is None:
        return "not a path"
    if text == ":memory:":
        return None
    if text[:5].lower() == "file:":
        uri = parse.urlsplit(text)
        if uri.path == ":memory:" or parse.parse_qs(uri.query).get("mode") == ["memory"]:
            return None
        if uri.netloc.lower() not in ("", "localhost"):
            return "not a local file"
        text = nturl2path.url2pathname(uri.path)
    if not text:
        return "SQLite's own temporary file"
    return write_problem(text, roots)


def _opens_for_writing(mode: object, flags: object) -> bool:
    if isinstance(flags, int):
        return bool(flags & ~_READ_FLAGS)
    if isinstance(mode, str):
        return any(letter in mode for letter in "wax+")
    return True


def _creates_for_writing(access: object, disposition: object) -> bool:
    return not isinstance(access, int) or bool(access & ~_READ_ACCESS) or disposition != _OPEN_EXISTING


def _check_write(event: str, target: object) -> None:
    problem = write_problem(target, GUARD.roots)
    if problem is not None:
        refuse(
            SafetyViolation("write_outside_the_data_folder", f"{event} {target!r}: {problem}"),
            transport="core",
            request=f"{event} {target!r}",
        )


def _check_read(event: str, target: object) -> None:
    if is_serial_device_path(target):
        refuse(
            SafetyViolation("serial_port_outside_stn_port", f"{target!r}; only lasto.safety.stn_port opens a serial port"),
            transport="stn",
            request=f"{event} {target!r}",
        )


def check_child_process(event: str, args: tuple[object, ...], *, test_run: bool) -> None:
    """A child process runs without this process's audit hook, so it could open anything: only a test run, which
    starts them, may. Nothing in src/ starts one (test_structure.py)."""
    if not test_run:
        refuse(
            SafetyViolation("child_process_refused", f"{event} {args[:2]!r}: a child process isn't covered by this process's guard"),
            transport="core",
            request=f"{event} {args[:2]!r}",
        )


def guard_event(event: str, args: tuple[object, ...]) -> None:
    """The audit hook: every write-capable open must pass write_problem(); a read that names a serial device is
    refused; a child process starts only in a test run; a Bluetooth socket never exists."""
    if event == "open":
        target, mode, flags = args[0], args[1], args[2]
        if _opens_for_writing(mode, flags):
            _check_write(event, target)
        else:
            _check_read(event, target)
    elif event == "os.truncate":
        _check_write(event, args[0])
    elif event == "_winapi.CreateFile":
        target, access, disposition = args[0], args[1], args[3]
        if _creates_for_writing(access, disposition):
            _check_write(event, target)
        else:
            _check_read(event, target)
    elif event == "sqlite3.connect":
        problem = sqlite_problem(args[0], GUARD.roots)
        if problem is not None:
            refuse(
                SafetyViolation("write_outside_the_data_folder", f"{event} {args[0]!r}: {problem}"),
                transport="core",
                request=f"{event} {args[0]!r}",
            )
    elif event == "sqlite3.load_extension" or (event == "sqlite3.enable_load_extension" and args[1]):
        refuse(
            SafetyViolation("sqlite_extension_refused", "SQLite extensions are native code the guard can't check"),
            transport="core",
            request=event,
        )
    elif event in CHILD_PROCESS_EVENTS:
        check_child_process(event, args, test_run=hardware_firewall_installed())
    elif event == "socket.__new__" and args[1] == _AF_BLUETOOTH:
        refuse(
            SafetyViolation("bluetooth_socket_refused", "a Bluetooth socket could reach the adapter with no serial port"),
            transport="stn",
            request=f"{event} family {args[1]}",
        )


# The data folder is the only place this process may write, so from here on Python writes no .pyc files.
# Install with `uv sync --locked --compile-bytecode`, so startup stays fast without them.
sys.dont_write_bytecode = True
sys.addaudithook(guard_event)
freeze(__name__)
