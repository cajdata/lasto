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

import pathlib

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


@pytest.mark.parametrize("event", ["open", "_winapi.CreateFile"])
def test_the_hook_refuses_and_audits_opening_a_serial_device(event, auditor, sink):
    REFUSALS.attach(auditor)
    with pytest.raises(SafetyViolation) as caught:
        serial_guard.refuse_serial_device_opens(event, (DEVICE_PATH, "r+b", 0))
    assert caught.value.reason == "serial_port_outside_stn_port"
    [refusal] = events(sink, "rejected")
    assert (refusal["reason"], refusal["transport"], refusal["request"]) == (
        "serial_port_outside_stn_port",
        "stn",
        f"{event} {DEVICE_PATH!r}",
    )


def test_the_hook_passes_everything_else(auditor, sink):
    REFUSALS.attach(auditor)
    serial_guard.refuse_serial_device_opens("open", ("audit.jsonl", "a", 0))
    serial_guard.refuse_serial_device_opens("open", (3, "rb", 0))
    serial_guard.refuse_serial_device_opens("os.listdir", ("COM5",))  # not an open
    assert events(sink, "rejected") == []


def test_ordinary_files_still_open(tmp_path):
    path = tmp_path / "record.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    assert path.read_text(encoding="utf-8") == "{}\n"
