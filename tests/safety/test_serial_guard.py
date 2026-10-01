r"""Finding P2: nothing but the STN adapter link opens a serial port.

On Windows the built-in open() or os.open() reaches a serial port without
pyserial ("COM5", or the device path \\.\COM5). The safety core installs an
audit hook the first time it is imported, and the hook refuses (audited) any
open of a serial device path. pyserial opens its port with CreateFileW through
ctypes, which raises no such event, so the adapter link is unaffected.

In this process the test firewall's own hook refuses these opens first, so the
safety core's hook is tested here by calling it. test_stn_reach.py tests both
hooks end to end, each in a subprocess.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest
from helpers import events

from lasto.safety import serial_guard
from lasto.safety.audit import REFUSALS
from lasto.safety.errors import SafetyViolation

DEVICE_PATH = r"\\.\COM5"


@pytest.mark.parametrize(
    "path",
    [
        "COM5",
        "com12",
        "COM5:",
        "COM5 ",
        "COM5.txt",  # Windows still sends a reserved name with an extension to the device
        r"C:\temp\COM1",
        DEVICE_PATH,
        "//./COM5",
        r"\\?\COM5",
        r"\??\COM5",
        "AUX",  # COM1 by another name
        "COM¹",
        r"\\.\GLOBALROOT\Device\Serial0",
        r"\\?\GLOBALROOT\Device\BthModem0",
        b"COM3",
        pathlib.PureWindowsPath(r"\\.\COM7"),
    ],
)
def test_serial_device_paths_are_recognized(path):
    assert serial_guard.is_serial_device_path(path)


@pytest.mark.parametrize(
    "path",
    [
        r"C:\Users\Chris\AppData\Local\lasto\audit.jsonl",
        "company.txt",
        "commit",
        "auxiliary.log",
        r"\\.\pipe\lasto",
        r"\\?\C:\a\long\path.txt",
        "nul",
        "",
        3,  # a file descriptor: already open
        None,
    ],
)
def test_ordinary_paths_are_not(path):
    assert not serial_guard.is_serial_device_path(path)


READS = {
    "open": (DEVICE_PATH, "r", os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT),
    "_winapi.CreateFile": (DEVICE_PATH, 0x80000000, 0, 3, 0),  # GENERIC_READ, OPEN_EXISTING
}


@pytest.mark.parametrize("event", sorted(READS))
def test_a_read_that_names_a_serial_device_is_refused_and_audited(event, auditor, sink):
    """The backstop for reads. A write is refused before this, by guard v2 (test_write_guard.py)."""
    REFUSALS.attach(auditor)
    with pytest.raises(SafetyViolation) as caught:
        serial_guard.guard_event(event, READS[event])
    assert caught.value.reason == "serial_port_outside_stn_port"
    [refusal] = events(sink, "rejected")
    assert (refusal["reason"], refusal["transport"], refusal["request"]) == (
        "serial_port_outside_stn_port",
        "stn",
        f"{event} {DEVICE_PATH!r}",
    )


def test_the_hook_passes_ordinary_reads(auditor, sink):
    REFUSALS.attach(auditor)
    serial_guard.guard_event("open", ("audit.jsonl", "r", os.O_RDONLY))
    serial_guard.guard_event("open", (3, "rb", os.O_RDONLY | os.O_BINARY))
    serial_guard.guard_event("os.listdir", ("COM5",))  # not an open
    assert events(sink, "rejected") == []


def test_importing_the_safety_core_stops_bytecode_writes():
    """Guard v2 (Phase 3) lets a process write only to its data folder, so Python must not write .pyc files
    after the guard installs. Install with `uv sync --locked --compile-bytecode` to keep startup fast."""
    probe = "import sys; print(sys.dont_write_bytecode); import lasto.safety; print(sys.dont_write_bytecode)"
    environment = {name: value for name, value in os.environ.items() if name != "PYTHONDONTWRITEBYTECODE"}
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True, env=environment)
    assert done.stdout.split() == ["False", "True"]


def test_ordinary_files_still_open(tmp_path):
    path = tmp_path / "record.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    assert path.read_text(encoding="utf-8") == "{}\n"
