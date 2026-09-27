"""pytest plugin: the hardware firewall and the simulator violation check.

Loaded by tests/conftest.py. The firewall is installed when this module is
imported, before any test module: loading PCANBasic.dll through ctypes, or
opening a serial port through pyserial, raises HardwareFirewallError. Any
test that leaves a simulator violation behind fails, and so does the run.
"""

from __future__ import annotations

import ctypes
import sys
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


def _refuse_serial(*args: object, **kwargs: object) -> None:
    raise HardwareFirewallError("tests may not open a serial port")


def install_firewall() -> None:
    ctypes.CDLL.__init__ = _guarded_cdll_init  # type: ignore[method-assign]
    fake_serial = types.ModuleType("serial")
    fake_serial.Serial = _refuse_serial  # type: ignore[attr-defined]
    fake_serial.serial_for_url = _refuse_serial  # type: ignore[attr-defined]
    sys.modules["serial"] = fake_serial


install_firewall()


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
