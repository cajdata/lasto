r"""Findings N1 and P2: the serial port stays inside the safety core.

Whoever holds the raw port can write anything to the adapter: no allowlist,
no audit, no silent-mode checks. So the STN module hands out no port. The
safety core opens it inside the adapter, and every attribute path from the
adapter to the port runs through a private attribute (N1).

Nor can anything else in the process open the port by name, with the built-in
open(), os.open(), or _winapi.CreateFile: the safety core's audit hook refuses
it, and so does the test firewall, on its own (P2). Each is tested in a
subprocess without the other. There, a backstop hook added last stops anything
that got past, so no attempt here ever reaches a device.
"""

import importlib
import subprocess
import sys

import pytest
from helpers import paths_to

from lasto.safety import stn_port
from lasto.safety.stn_port import StnAdapter
from lasto.sim.fake_stn import FakeStnPort
from lasto.sim.pytest_plugin import HardwareFirewallError


def test_the_stn_module_hands_out_no_port():
    public = {name for name in vars(stn_port) if not name.startswith("_")}
    assert "open_serial" not in public
    assert "open_adapter" in public


def _open(auditor, ports):
    def factory(**settings):
        port = FakeStnPort()
        ports.append((port, settings))
        return port

    return importlib.import_module("lasto.safety.stn_port").open_adapter("COM5", auditor=auditor, factory=factory)


def test_the_opened_port_is_reachable_only_through_private_attributes(auditor):
    ports = []
    stn = _open(auditor, ports)
    assert isinstance(stn, StnAdapter)
    [(port, settings)] = ports
    assert settings == {"port": "COM5", "baudrate": 115200, "timeout": 2.0, "write_timeout": 2.0}
    hits = paths_to(stn, lambda obj: obj is port)
    assert hits, "the adapter should hold its port"
    assert all(path.split(".")[1].startswith("_") for path, _via, _obj in hits)
    public = [getattr(stn, name) for name in dir(stn) if not name.startswith("_")]
    assert all(value is not port for value in public)


def test_everything_the_adapter_sends_goes_through_its_allowlist(auditor, sink):
    ports = []
    stn = _open(auditor, ports)
    stn.reset()
    [(port, _settings)] = ports
    sent = [r["command"] for r in sink.records if r["event"] == "adapter_command"]
    assert port.commands == sent  # every line on the port was checked and audited first


def test_opening_a_real_port_is_blocked_in_tests(auditor):
    with pytest.raises(HardwareFirewallError):
        importlib.import_module("lasto.safety.stn_port").open_adapter("COM5", auditor=auditor)


# ---- opening the port by name, around the adapter (finding P2) ----

BACKSTOP = r"""
import sys

def backstop(event, args):  # runs after every hook added before it; none of the attempts below may get this far
    if event in ("open", "_winapi.CreateFile") and "COM250" in str(args[0]).upper():
        raise SystemExit(f"backstop: nothing refused {event} {args[0]!r}")

sys.addaudithook(backstop)
"""

ATTEMPTS = r"""
import _winapi
import os
import pathlib

attempts = [
    lambda: open(r"\\.\COM250", "r+b", buffering=0),
    lambda: os.open("COM250", os.O_RDWR),
    lambda: pathlib.Path("COM250").write_bytes(b"0100\r"),
    lambda: _winapi.CreateFile(r"\\.\COM250", 0xC0000000, 0, 0, 3, 0, 0),
    lambda: open("COM250", "rb", buffering=0),  # read-only: no write, but it still names a serial port
]
for attempt in attempts:
    try:
        attempt()
    except REFUSED as error:
        print(type(error).__name__, getattr(error, "reason", "-"))
try:
    open(r"\\.\pipe\lasto-no-such-pipe", "rb")
except FileNotFoundError:
    print("pipe not refused")
print("safety core loaded" if "lasto.safety" in sys.modules else "safety core not loaded")
"""


def run(setup: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", setup + BACKSTOP + ATTEMPTS], capture_output=True, text=True)


def test_the_safety_core_refuses_serial_opens_anywhere_in_the_process():
    """A process that has imported the safety core, with no test firewall. Guard v2 refuses every write-capable
    open outside the data folder, which this process never registered; a read that names a port meets the
    name check, the backstop for reads."""
    done = run("import lasto.safety\nfrom lasto.safety.errors import SafetyViolation as REFUSED\n")
    assert done.stdout.splitlines() == [
        *["SafetyViolation write_outside_the_data_folder"] * 4,
        "SafetyViolation serial_port_outside_stn_port",
        "pipe not refused",
        "safety core loaded",
    ], done.stderr
    assert "backstop" not in done.stderr
    assert done.stderr.count('"reason": "write_outside_the_data_folder"') == 4  # held, then reported at exit
    assert done.stderr.count('"reason": "serial_port_outside_stn_port"') == 1


def test_the_test_firewall_refuses_serial_opens_on_its_own():
    """The firewall's own check, written separately from the safety core's, in a process that never loads the core."""
    done = run("import lasto.sim.pytest_plugin\nfrom lasto.sim.pytest_plugin import HardwareFirewallError as REFUSED\n")
    assert done.stdout.splitlines() == [
        *["HardwareFirewallError -"] * 5,  # reads and writes alike: the firewall's check is by name
        "pipe not refused",
        "safety core not loaded",  # pytest loads the plugin before coverage starts; it must not import the core
    ], done.stderr
    assert "backstop" not in done.stderr
