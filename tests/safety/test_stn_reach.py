"""Finding N1: the serial port stays inside the safety core.

Whoever holds the raw port can write anything to the adapter: no allowlist,
no audit, no silent-mode checks. So the STN module hands out no port. The
safety core opens it inside the adapter, and every attribute path from the
adapter to the port runs through a private attribute.
"""

import importlib

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
