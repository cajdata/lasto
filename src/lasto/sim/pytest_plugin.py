"""pytest plugin: the hardware firewall and the simulator violation check.

Loaded only by this repo's own pytest configuration (`-p lasto.sim.pytest_plugin`
in pyproject.toml's addopts). It is not a pytest11 entry point, so installing
lasto never changes anyone else's test runs; tests/test_packaging.py holds
that. The firewall is installed when this module is
imported, before any test module: loading PCANBasic.dll through ctypes, or
opening a serial port through pyserial or by name (open("COM5")), raises
HardwareFirewallError. Any
test that leaves a simulator violation behind fails, and so does the run.

The process kill switch latches for the life of the process, and a test run
is one process, so each test starts with it clear. The safety core allows
that reset only while this firewall is installed, and audits every one.
Nothing in src/ may import this module.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
import tempfile
import types
from collections.abc import Iterator

import pytest

from lasto.sim.violations import VIOLATIONS


class HardwareFirewallError(RuntimeError):
    """A test tried to reach real hardware."""


_real_cdll_init = ctypes.CDLL.__init__


def _guarded_cdll_init(self: ctypes.CDLL, name: str | None, *args: object, **kwargs: object) -> None:
    if name is not None and "pcanbasic" in str(name).lower():
        raise HardwareFirewallError(f"tests may not load {name}")
    _real_cdll_init(self, name, *args, **kwargs)  # type: ignore[arg-type]


# The mark lasto.safety.pcan_dll.hardware_firewall_installed() looks for.
_guarded_cdll_init.lasto_hardware_firewall = True  # type: ignore[attr-defined]


def _refuse_serial(*args: object, **kwargs: object) -> None:
    raise HardwareFirewallError("tests may not open a serial port")


# A serial port named anywhere in a path (COM5, COM5.txt, \\.\COM5, AUX), or GLOBALROOT, which reaches any device.
# Written separately from the safety core's check (lasto.safety.serial_guard), like the simulator's oracle.
_SERIAL_PATH = re.compile(
    r"(?i)(?:^|[\\/])(?:com[0-9¹²³]+|aux)(?:[.:][^\\/]*)?\s*(?:[\\/]|$)|(?:^|[\\/])globalroot(?:[\\/]|$)"
)


def _refuse_serial_paths(event: str, args: tuple[object, ...]) -> None:
    """Audit hook: tests may not open a serial port by name, with open(), os.open(), or _winapi.CreateFile."""
    if event in ("open", "_winapi.CreateFile") and isinstance(args[0], str | bytes | os.PathLike):
        path = os.fsdecode(os.fspath(args[0]))
        if _SERIAL_PATH.search(path):
            raise HardwareFirewallError(f"tests may not open a serial port: {path!r}")


def install_firewall() -> None:
    ctypes.CDLL.__init__ = _guarded_cdll_init  # type: ignore[method-assign]
    fake_serial = types.ModuleType("serial")
    fake_serial.Serial = _refuse_serial  # type: ignore[attr-defined]
    fake_serial.serial_for_url = _refuse_serial  # type: ignore[attr-defined]
    sys.modules["serial"] = fake_serial
    sys.addaudithook(_refuse_serial_paths)  # finding P2: the built-in open() reaches a port without pyserial


install_firewall()

# The test run's temp folder, asked now, as the plugin loads and before anything can import the safety core:
# tempfile.gettempdir() checks the folder by writing a file in it, which guard v2 refuses until the folder is
# registered. With output capture on, pytest asks first anyway; with -s, nothing would.
_TEMP_FOLDER = os.path.realpath(tempfile.gettempdir())


def fresh_kill_switch() -> None:
    """Clear the process kill switch. For a test that runs many examples in one test (Hypothesis)."""
    # Imported here, not above: pytest loads this plugin before coverage starts measuring the safety core.
    from lasto.safety.killswitch import reset_for_tests

    reset_for_tests()


@pytest.fixture(autouse=True)
def _fresh_kill_switch() -> None:
    fresh_kill_switch()


def pytest_sessionstart(session: pytest.Session) -> None:
    """Guard v2 lets a process write only in the folders it registered. A test run registers its own: the system
    temp folder, where pytest keeps its temp folders and capture files, and the repo's tool caches. Only a test
    run (the hardware firewall installed) may register more than one.

    Done as the session starts, after coverage has started measuring and before collection, so a run whose
    collection fails still writes pytest's cache and reports its error."""
    from lasto.safety.serial_guard import allow_writes_in

    allow_writes_in(_TEMP_FOLDER)
    for name in (".pytest_cache", ".hypothesis"):
        folder = session.config.rootpath / name
        folder.mkdir(exist_ok=True)
        allow_writes_in(folder)


@pytest.fixture(autouse=True)
def _no_simulator_violations() -> Iterator[None]:
    VIOLATIONS.take()
    yield
    found = VIOLATIONS.take()
    if found:
        pytest.fail("the simulator received forbidden traffic:\n  " + "\n  ".join(found), pytrace=False)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    leftover = VIOLATIONS.take()
    if leftover:
        print("\nsimulator violations outside any test:\n  " + "\n  ".join(leftover))
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
