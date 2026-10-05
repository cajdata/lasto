"""lasto adapter: the OBDLink MX+ on its own, for bench test B3, against the simulated MX+ (lasto.sim.fake_stn).

Everything the adapter is sent goes through the safety core's STN link, which these tests don't re-check; the
simulator records a violation, failing the test, for anything that would reach the vehicle bus, change the adapter's
saved settings, monitor without silent mode, or arrive during the bootloader window.
"""

from __future__ import annotations

import json
import signal

import pytest

from lasto.operations import adapter
from lasto.sim.clock import FakeClock
from lasto.sim.fake_stn import FakeStnPort
from lasto.sim.pytest_plugin import HardwareFirewallError
from lasto.storage.root import DataRoot


@pytest.fixture
def root(tmp_path) -> DataRoot:
    root = DataRoot(tmp_path / "data")
    root.ensure()
    return root


def simulated(**port_options) -> tuple[adapter.AdapterSource, FakeStnPort]:
    port = FakeStnPort(clock=FakeClock(), **port_options)
    return adapter.simulated(port), port


def audit_records(root: DataRoot) -> list[dict]:
    [log] = [path for path in root.audit_dir.glob("*-adapter-*.jsonl")]
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_a_check_opens_waits_for_the_adapter_resets_and_identifies(root):
    source, port = simulated()
    said: list[str] = []
    result = adapter.check(root, source, monitor=None, seconds=None, report=said.append)
    assert result.end_reason == "done"
    assert result.banner == "ELM327 v1.4b"
    assert result.identity == {"firmware": "STN2255 v5.10.3", "device": "OBDLink MX+ r3.2.1", "serial": "123456789012"}
    assert result.voltage == 12.63
    assert result.settled_after == pytest.approx(3.0)  # the simulated MX+ stays quiet, so the open waits it out
    assert port.commands == ["ATZ", "ATE0", "ATL0", "ATS0", "ATH1", "ATM0", "STI", "STDI", "STSN", "STVR"]
    assert port.closed
    text = "\n".join(said)
    assert "settled after 3.00 s" in text and "ELM327 v1.4b" in text and "12.63 V" in text
    commands = [record["command"] for record in audit_records(root) if record["event"] == "adapter_command"]
    assert commands == port.commands


def test_the_default_simulator_runs_on_its_own_clock(root):
    result = adapter.check(root, adapter.simulated(), monitor="can", seconds=5.0, report=lambda line: None)
    assert result.end_reason == "time_limit" and result.lines > 0


def test_a_can_monitor_prints_what_it_hears_until_the_time_limit(root):
    source, port = simulated(monitor_lines=["0250F5A000000000000", "2C40011223344556677"])
    said: list[str] = []
    result = adapter.check(root, source, monitor="can", seconds=5.0, report=said.append)
    assert result.end_reason == "time_limit"
    assert result.lines == 2
    assert port.commands[-5:] == ["ATPPS", "STP31", "STPR", "STCMM0", "STMA"]
    assert port.written.endswith(b"\x08")  # the backspace stop
    assert not port.monitoring and port.closed
    text = "\n".join(said)
    assert "0250F5A000000000000" in text and "2C40011223344556677" in text
    assert "the prompt came back" in text


def test_a_kline_monitor_uses_the_quiet_preset(root):
    source, port = simulated(monitor_lines=["8110F12101A4"])
    result = adapter.check(root, source, monitor="kline", seconds=5.0, report=lambda line: None)
    assert result.end_reason == "time_limit" and result.lines == 1
    assert port.commands[-4:] == ["ATSW00", "STP23", "STPR", "STMA"]


def test_a_monitor_that_ends_on_its_own(root):
    source, _ = simulated(monitor_lines=["0250F5A000000000000"], monitor_ends=True)
    result = adapter.check(root, source, monitor="can", seconds=60.0, report=lambda line: None)
    assert result.end_reason == "monitor_ended"


class StopsAfter(FakeStnPort):
    """Raises a Ctrl+C or Ctrl+Break the moment the first monitor line has been read."""

    def __init__(self, signum: int, **options) -> None:
        super().__init__(**options)
        self.signum = signum

    def read_until(self, expected=b"\n", size=None):
        data = super().read_until(expected, size)
        if self.monitoring and data.endswith(b"\r") and self.signum is not None:
            signum, self.signum = self.signum, None
            signal.raise_signal(signum)
        return data


@pytest.mark.parametrize(("signum", "reason"), [(signal.SIGINT, "ctrl_c"), (signal.SIGBREAK, "ctrl_break")])
def test_ctrl_c_and_ctrl_break_stop_the_monitor_cleanly(root, signum, reason):
    port = StopsAfter(signum, clock=FakeClock(), monitor_lines=["0250F5A000000000000"] * 5)
    result = adapter.check(root, adapter.simulated(port), monitor="can", seconds=60.0, report=lambda line: None)
    assert result.end_reason == reason
    assert port.written.endswith(b"\x08") and port.closed
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler  # put back


class DropsTheLink(FakeStnPort):
    """Bluetooth goes off after the monitor starts: what pyserial raises is an OSError."""

    def read_until(self, expected=b"\n", size=None):
        if self.monitoring:
            raise OSError("ClearCommError failed (PermissionError(13, 'The device does not recognize the command.'))")
        return super().read_until(expected, size)


def test_a_dropped_link_ends_the_run_with_no_retry(root):
    port = DropsTheLink(clock=FakeClock())
    said: list[str] = []
    result = adapter.check(root, adapter.simulated(port), monitor="can", seconds=60.0, report=said.append)
    assert result.end_reason == "adapter_error"
    written = bytes(port.written)
    assert port.closed and written.endswith(b"STMA\r")  # nothing after the drop: no stop, no retry
    assert "nothing retries it" in "\n".join(said)
    assert [r["reason"] for r in audit_records(root) if r["event"] == "rejected"] == ["adapter_link_lost"]


class NoPromptAfterStop(FakeStnPort):
    """The adapter still sends its monitor output after the backspace: the prompt never comes back."""

    def write(self, data):
        if data == b"\x08":
            self.written += data
            return 1
        return super().write(data)

    def read_until(self, expected=b"\n", size=None):
        if self.written.endswith(b"\x08"):
            return b"0250F5A0000"
        return super().read_until(expected, size)


def test_the_adapter_is_closed_whatever_stops_the_check(root):
    """Even when the output fails mid-monitor, and the monitor then won't stop: the adapter is still closed."""
    port = NoPromptAfterStop(clock=FakeClock(), monitor_lines=["0250F5A000000000000"])
    said: list[str] = []

    def report(line: str) -> None:
        if line.lstrip().startswith("0.0"):  # the first monitor line
            raise RuntimeError("the console went away")
        said.append(line)

    with pytest.raises(RuntimeError, match="console"):
        adapter.check(root, adapter.simulated(port), monitor="can", seconds=60.0, report=report)
    assert port.closed and port.written.endswith(b"\x08")
    assert any(line.startswith("Closing the adapter: ") and "no prompt" in line for line in said)


def test_an_adapter_that_will_not_settle_is_never_written_to(root):
    source, port = simulated(on_open="\x00\x00ELM3")
    said: list[str] = []
    result = adapter.check(root, source, monitor=None, seconds=None, report=said.append)
    assert result.end_reason == "adapter_error"
    assert port.written == b"" and port.closed
    assert "power-cycle" in "\n".join(said)


def test_opening_the_adapter_after_it_rebooted_waits_for_its_prompt(root):
    source, port = simulated(on_open="\r\rELM327 v1.4b\r\r>")
    result = adapter.check(root, source, monitor=None, seconds=None, report=lambda line: None)
    assert result.end_reason == "done"
    assert result.settled_after < 3.0  # the prompt came, so the open didn't wait out the settle time


def test_a_simulated_mx_plus_needs_a_clock():
    """Without one, a read that hears nothing takes no time, and a monitor would never reach its time limit."""
    with pytest.raises(ValueError, match="clock"):
        adapter.simulated(FakeStnPort())


def test_the_monitor_is_can_or_kline(root):
    source, _ = simulated()
    with pytest.raises(ValueError, match="can, kline"):
        adapter.check(root, source, monitor="obd", seconds=5.0, report=lambda line: None)


def test_a_simulated_monitor_needs_a_time_limit(root):
    source, _ = simulated()
    with pytest.raises(ValueError, match="time limit"):
        adapter.check(root, source, monitor="can", seconds=None, report=lambda line: None)


def test_the_bench_source_opens_a_real_port_which_tests_never_reach(root):
    source = adapter.bench("COM5")
    assert source.live and source.port == "COM5"
    with pytest.raises(HardwareFirewallError):
        adapter.check(root, source, monitor=None, seconds=None, report=lambda line: None)
