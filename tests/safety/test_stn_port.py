"""Rule 4: the STN adapter link against the simulated MX+."""

import itertools

import pytest
from helpers import events

from lasto.safety import stn_port
from lasto.safety.audit import REFUSALS
from lasto.safety.errors import AdapterError, SafetyViolation
from lasto.safety.stn_port import COMMAND_TIMEOUT, StnAdapter, open_adapter
from lasto.sim.fake_stn import FakeStnPort
from lasto.sim.pytest_plugin import HardwareFirewallError


def adapter(auditor, **port_options):
    port = FakeStnPort(**port_options)
    return StnAdapter(port, auditor), port


def ready(auditor, **port_options):
    stn, port = adapter(auditor, **port_options)
    stn.reset()
    return stn, port


def test_open_adapter_checks_the_port_name(auditor):
    for bad in ["COM0", "COM", "com5", "/dev/ttyUSB0", "COM5 ", None, "COM1234"]:
        with pytest.raises(ValueError):
            open_adapter(bad, auditor=auditor, factory=lambda **kwargs: FakeStnPort())


def test_open_adapter_uses_the_factory(auditor):
    seen = {}

    def factory(**kwargs):
        seen.update(kwargs)
        return FakeStnPort()

    assert isinstance(open_adapter("COM5", auditor=auditor, factory=factory), StnAdapter)
    assert seen == {"port": "COM5", "baudrate": 115200, "timeout": COMMAND_TIMEOUT, "write_timeout": COMMAND_TIMEOUT}


def test_opening_a_real_port_is_blocked_in_tests(auditor):
    with pytest.raises(HardwareFirewallError):
        open_adapter("COM5", auditor=auditor)


_PROBES = itertools.count()


def _still_attached(sink) -> bool:
    """Whether the refusal log still writes to this sink: an event recorded now reaches it only if it is."""
    probe = f"attached_probe_{next(_PROBES)}"
    REFUSALS.event(probe)
    return events(sink, probe) != []


def test_a_refused_port_name_goes_to_the_callers_audit_log(auditor, sink):
    """Review finding L4: the audit log is attached before the port opens, not held for the next one."""
    with pytest.raises(ValueError):
        open_adapter("COM0", auditor=auditor, factory=lambda **kwargs: FakeStnPort())
    assert [r["reason"] for r in events(sink, "rejected")] == ["bad_port_name"]
    assert not _still_attached(sink)


class PortWontOpen(OSError):
    """pyserial's SerialException: could not open port."""


def test_a_port_that_will_not_open_is_audited(auditor, sink):
    def factory(**kwargs):
        raise PortWontOpen("could not open port 'COM5': FileNotFoundError(2, 'The system cannot find the file specified.')")

    with pytest.raises(AdapterError, match="could not open COM5"):
        open_adapter("COM5", auditor=auditor, factory=factory)
    [refusal] = events(sink, "rejected")
    assert (refusal["reason"], refusal["request"]) == ("adapter_open_failed", "open COM5")
    assert not _still_attached(sink)


def test_an_open_adapter_holds_the_audit_log_once(auditor, sink):
    stn = open_adapter("COM5", auditor=auditor, factory=lambda **kwargs: FakeStnPort())
    assert _still_attached(sink)
    stn.close()
    assert not _still_attached(sink)


def test_reset_waits_for_the_prompt_then_configures(auditor, sink):
    stn, port = adapter(auditor)
    assert stn.reset() == "ELM327 v1.4b"
    assert port.commands == ["ATZ", "ATE0", "ATL0", "ATS0", "ATH1", "ATM0"]
    assert port.timeout == COMMAND_TIMEOUT
    assert [r["command"] for r in events(sink, "adapter_command")] == port.commands


def test_everything_needs_a_reset_first(auditor):
    stn, _ = adapter(auditor)
    for call in (stn.identify, stn.read_voltage, stn.programmable_parameters, stn.start_kline_monitor):
        with pytest.raises(AdapterError, match="reset"):
            call()


def test_identify_and_voltage(auditor):
    stn, _ = ready(auditor)
    assert stn.identify() == {"firmware": "STN2255 v5.10.3", "device": "OBDLink MX+ r3.2.1", "serial": "123456789012"}
    assert stn.read_voltage() == 12.63


@pytest.mark.parametrize("reading", ["--.--", "12.6V", "", "123.4"])
def test_unexpected_voltage_readings(auditor, reading):
    stn, _ = ready(auditor, voltage=reading)
    with pytest.raises(AdapterError):
        stn.read_voltage()


def test_can_monitor_runs_the_silent_checks_first(auditor):
    stn, port = ready(auditor, monitor_lines=["7E8 03 41 0D 00", ""])
    stn.start_can_monitor()
    assert port.commands[-5:] == ["ATPPS", "STP31", "STPR", "STCMM0", "STMA"]
    assert stn.monitoring
    assert stn.read_monitor_line() == "7E8 03 41 0D 00"
    assert stn.read_monitor_line() == ""
    assert stn.stop_monitor() == ["STOPPED"]
    assert not stn.monitoring
    assert stn.stop_monitor() == []


def test_can_monitor_protocol_33(auditor):
    stn, port = ready(auditor)
    stn.start_can_monitor("33")
    assert "STP33" in port.commands
    stn.stop_monitor()


def test_can_monitor_refused_when_the_adapter_acks_by_default(auditor):
    stn, port = ready(auditor, pp21=(0x00, True))
    with pytest.raises(AdapterError, match="PP 21"):
        stn.start_can_monitor()
    assert "STMA" not in port.commands


def test_can_monitor_refused_when_the_protocol_does_not_read_back(auditor):
    stn, port = ready(auditor, protocol_report="0")
    with pytest.raises(AdapterError, match="STPR"):
        stn.start_can_monitor()
    assert "STMA" not in port.commands


@pytest.mark.parametrize("protocol", ["32", "0", "21"])
def test_can_monitor_protocols(auditor, protocol):
    stn, _ = ready(auditor)
    with pytest.raises(ValueError):
        stn.start_can_monitor(protocol)


def test_kline_monitor(auditor):
    stn, port = ready(auditor, monitor_lines=["81 10 F1 21 01 A4"])
    stn.start_kline_monitor()
    assert port.commands[-4:] == ["ATSW00", "STP23", "STPR", "STMA"]
    assert stn.read_monitor_line() == "81 10 F1 21 01 A4"
    stn.stop_monitor()
    stn.start_kline_monitor("21")
    stn.stop_monitor()


@pytest.mark.parametrize("protocol", ["22", "24", "25", "31"])
def test_kline_monitor_never_uses_autoinit_presets(auditor, protocol):
    stn, _ = ready(auditor)
    with pytest.raises(ValueError):
        stn.start_kline_monitor(protocol)


def test_monitor_that_ends_on_its_own(auditor):
    stn, _ = ready(auditor, monitor_lines=["7E8 03 41 0D 00"], monitor_ends=True)
    stn.start_can_monitor()
    assert stn.read_monitor_line() == "7E8 03 41 0D 00"
    assert stn.read_monitor_line() == "BUFFER FULL"
    assert stn.read_monitor_line() == ""
    assert stn.read_monitor_line() is None  # the prompt came back
    assert not stn.monitoring
    assert stn.stop_monitor() == []
    with pytest.raises(AdapterError, match="isn't monitoring"):
        stn.read_monitor_line()


def test_partial_monitor_lines_are_held_until_complete(auditor):
    stn, port = ready(auditor)
    stn.start_can_monitor()
    port._out += b"7E8 03 4"
    assert stn.read_monitor_line() is None
    port._out += b"1 0D 00\r"
    assert stn.read_monitor_line() == "7E8 03 41 0D 00"
    stn.stop_monitor()


def test_no_commands_while_monitoring(auditor, sink):
    stn, port = ready(auditor)
    stn.start_can_monitor()
    with pytest.raises(SafetyViolation) as refused:
        stn.read_voltage()
    assert refused.value.reason == "adapter_is_monitoring"
    assert events(sink, "rejected")[-1]["reason"] == "adapter_is_monitoring"
    assert stn.monitoring
    stn.stop_monitor()


def test_refused_commands_are_audited_and_never_written(auditor, sink):
    stn, port = ready(auditor)
    written = bytes(port.written)
    for command in ("STPX H:7E0,D:0101", "0100", "ATZ", "STMA", "stm"):
        with pytest.raises(SafetyViolation):
            stn._command(command)
    assert bytes(port.written) == written
    reasons = [r["reason"] for r in events(sink, "rejected")]
    assert reasons == [
        "adapter_command_characters",
        "adapter_hex_request",
        "adapter_reset_outside_reset_routine",
        "adapter_monitor_outside_monitor_routine",
        "adapter_monitor_outside_monitor_routine",
    ]


class SilentPort(FakeStnPort):
    """Answers a command, but never with a prompt."""

    def read_until(self, expected=b"\n", size=None):
        return b"OK\r" if self.commands else b""


def test_missing_prompt(auditor):
    stn = StnAdapter(SilentPort(), auditor)
    with pytest.raises(AdapterError, match="no prompt"):
        stn.reset()


def test_expect_reports_a_mismatch(auditor):
    stn, port = ready(auditor)
    port.pp[0x21] = (0xFF, False)
    port.protocol_report = "33"
    with pytest.raises(AdapterError, match="expected '31'"):
        stn.start_can_monitor("31")


def test_echo_is_stripped_before_ate0_takes_effect(auditor):
    stn, port = adapter(auditor)
    stn._configured = True  # skip the reset to see the echo
    stn._write_command("ATZ", reset=True)
    stn._read_prompt("ATZ")
    port.echo = True
    assert stn._command("STI") == "STN2255 v5.10.3"


def test_close(auditor):
    stn, port = adapter(auditor)
    stn.close()
    assert port.closed


def test_lines_helper():
    assert stn_port._lines("a\r\rb\n c \r") == ["a", "b", "c"]


# ---- the bootloader window and an adapter in an unknown state (finding E, Phase 3 A7) ----

BOOTED = "\r\rELM327 v1.4b\r\r>"


def test_opening_sends_nothing_until_the_adapter_has_booted(auditor):
    """Opening the link can reboot the adapter. The simulator records any byte sent before its prompt."""
    stn, port = adapter(auditor, on_open=BOOTED)
    assert port.written == b""
    assert stn.reset() == "ELM327 v1.4b"


def test_a_quiet_adapter_is_ready_once_the_settle_time_passes(auditor):
    stn, port = adapter(auditor)
    assert port.timeout == COMMAND_TIMEOUT
    stn.reset()


class SettleTimes(FakeStnPort):
    def __init__(self, **options):
        self.timeouts = []
        super().__init__(**options)

    def read_until(self, expected=b"\n", size=None):
        self.timeouts.append(self.timeout)
        return super().read_until(expected, size)


def test_the_first_read_waits_the_settle_time(auditor):
    port = SettleTimes()
    StnAdapter(port, auditor)
    assert port.timeouts == [stn_port.SETTLE_TIMEOUT]
    assert port.timeout == COMMAND_TIMEOUT


def test_an_adapter_that_sends_without_a_prompt_is_never_written_to(auditor, sink):
    port = FakeStnPort(on_open="\x00\x00ELM3")  # still booting, or streaming output
    with pytest.raises(AdapterError) as refused:
        StnAdapter(port, auditor)
    assert [r["reason"] for r in events(sink, "rejected")] == ["adapter_not_settled"]
    assert "power-cycle" in str(refused.value)
    assert port.written == b"" and port.closed
    assert not _still_attached(sink)  # the failed open detached its audit log


def test_after_a_prompt_timeout_the_adapter_refuses_until_reopened(auditor, sink):
    port = SilentPort()
    stn = StnAdapter(port, auditor)
    with pytest.raises(AdapterError, match="no prompt"):
        stn.reset()
    calls = (stn.reset, stn.identify, stn.start_kline_monitor, stn.start_can_monitor, lambda: stn._command("STI"))
    for call in calls:
        with pytest.raises(AdapterError, match="opened again"):
            call()
    assert port.commands == ["ATZ"]  # ATZ is never sent again blindly
    assert [r["reason"] for r in events(sink, "rejected")] == ["adapter_no_prompt"] + ["adapter_state_unknown"] * len(calls)


def test_a_banner_mid_session_means_the_adapter_rebooted(auditor, sink):
    stn, port = ready(auditor)
    port._out += b"ELM327 v1.4b\r\r>"  # it rebooted: power, or a Bluetooth reconnect
    with pytest.raises(AdapterError, match="rebooted"):
        stn.identify()
    sent = list(port.commands)
    with pytest.raises(AdapterError):
        stn.read_voltage()
    with pytest.raises(AdapterError):
        stn.reset()
    assert port.commands == sent
    assert [r["reason"] for r in events(sink, "rejected")] == ["adapter_rebooted", "adapter_state_unknown", "adapter_state_unknown"]


def test_a_banner_in_monitor_output_means_the_adapter_rebooted(auditor, sink):
    stn, port = ready(auditor)
    stn.start_can_monitor()
    port._out += b"ELM327 v1.4b\r"
    with pytest.raises(AdapterError, match="rebooted"):
        stn.read_monitor_line()
    assert not stn.monitoring
    with pytest.raises(AdapterError):
        stn.read_monitor_line()
    assert [r["reason"] for r in events(sink, "rejected")] == ["adapter_rebooted", "adapter_state_unknown"]


def test_a_banner_while_stopping_the_monitor_means_the_adapter_rebooted(auditor, sink):
    stn, port = ready(auditor)
    stn.start_can_monitor()
    port.monitoring = False  # it rebooted: the backspace stops nothing, and the banner comes back
    port._out += b"ELM327 v1.4b\r\r>"
    with pytest.raises(AdapterError, match="rebooted"):
        stn.stop_monitor()
    assert [r["reason"] for r in events(sink, "rejected")] == ["adapter_rebooted"]


class SerialException(OSError):
    """What pyserial raises when the Bluetooth link drops: ClearCommError or WriteFile failed."""


class DropsTheLink(FakeStnPort):
    def __init__(self, **options):
        self.dropped = False
        super().__init__(**options)

    def write(self, data):
        if self.dropped:
            raise SerialException("WriteFile failed (PermissionError(13, 'The device does not recognize the command.'))")
        return super().write(data)

    def read_until(self, expected=b"\n", size=None):
        if self.dropped:
            raise SerialException("ClearCommError failed (PermissionError(13, 'The device does not recognize the command.'))")
        return super().read_until(expected, size)


@pytest.mark.parametrize("when", ["a command", "a monitor read", "stopping the monitor"])
def test_a_dropped_link_closes_the_adapter_with_no_retry(auditor, sink, when):
    port = DropsTheLink()
    stn = StnAdapter(port, auditor)
    stn.reset()
    action = stn.identify
    if when != "a command":
        stn.start_can_monitor()
        action = stn.read_monitor_line if when == "a monitor read" else stn.stop_monitor
    port.dropped = True
    written = bytes(port.written)
    with pytest.raises(AdapterError, match="nothing retries"):
        action()
    assert port.closed and not stn.monitoring
    with pytest.raises(AdapterError):
        stn.identify()
    assert bytes(port.written) == written
    assert [r["reason"] for r in events(sink, "rejected")] == ["adapter_link_lost", "adapter_closed"]


def test_a_link_that_drops_while_opening_closes_it(auditor, sink):
    port = DropsTheLink()
    port.dropped = True
    with pytest.raises(AdapterError, match="nothing retries"):
        StnAdapter(port, auditor)
    assert port.closed
    assert [r["reason"] for r in events(sink, "rejected")] == ["adapter_link_lost"]


class CloseFails(DropsTheLink):
    def close(self):
        raise SerialException("the port is already gone")


def test_a_dropped_link_is_reported_even_if_closing_the_port_fails(auditor, sink):
    port = CloseFails()
    stn = StnAdapter(port, auditor)
    stn.reset()
    port.dropped = True
    with pytest.raises(AdapterError, match="nothing retries"):
        stn.identify()
    assert [r["reason"] for r in events(sink, "rejected")] == ["adapter_link_lost"]


def test_a_closed_adapter_refuses_everything(auditor, sink):
    stn, port = ready(auditor)
    stn.close()
    REFUSALS.attach(auditor)
    with pytest.raises(AdapterError, match="closed"):
        stn.identify()
    assert [r["reason"] for r in events(sink, "rejected")] == ["adapter_closed"]


# ---- finding #3: every adapter rejection is audited where it is raised, exactly once ----


class RefusesToConfigure(FakeStnPort):
    def _command(self, text):
        return "?" if text == "ATL0" else super()._command(text)


def _not_reset(call):
    return lambda auditor: call(adapter(auditor)[0])


def _ready(call, **port_options):
    return lambda auditor: call(ready(auditor, **port_options)[0])


def _monitor_ended(auditor):
    stn, _ = ready(auditor, monitor_lines=[], monitor_ends=True)
    stn.start_can_monitor()
    while stn.monitoring:
        stn.read_monitor_line()
    stn.read_monitor_line()


REJECTIONS = [
    ("identify before a reset", _not_reset(lambda stn: stn.identify()), "adapter_not_reset"),
    ("voltage before a reset", _not_reset(lambda stn: stn.read_voltage()), "adapter_not_reset"),
    ("parameters before a reset", _not_reset(lambda stn: stn.programmable_parameters()), "adapter_not_reset"),
    ("K-line monitor before a reset", _not_reset(lambda stn: stn.start_kline_monitor()), "adapter_not_reset"),
    ("CAN monitor before a reset", _not_reset(lambda stn: stn.start_can_monitor()), "adapter_not_reset"),
    ("a configure step answered wrongly", lambda auditor: StnAdapter(RefusesToConfigure(), auditor).reset(), "adapter_unexpected_answer"),
    ("an unreadable voltage", _ready(lambda stn: stn.read_voltage(), voltage="--.--"), "adapter_bad_voltage"),
    ("PP 21 ACKs by default", _ready(lambda stn: stn.start_can_monitor(), pp21=(0x00, True)), "adapter_acks_by_default"),
    ("protocol doesn't read back", _ready(lambda stn: stn.start_can_monitor(), protocol_report="0"), "adapter_unexpected_answer"),
    ("K-line protocol doesn't read back", _ready(lambda stn: stn.start_kline_monitor(), protocol_report="0"), "adapter_unexpected_answer"),
    ("no prompt", lambda auditor: StnAdapter(SilentPort(), auditor).reset(), "adapter_no_prompt"),
    ("reading a monitor that ended", _monitor_ended, "adapter_not_monitoring"),
]


@pytest.mark.parametrize(("what", "action", "reason"), REJECTIONS, ids=[r[0] for r in REJECTIONS])
def test_every_adapter_rejection_is_audited_once(auditor, sink, what, action, reason):
    with pytest.raises(AdapterError):
        action(auditor)
    rejected = events(sink, "rejected")
    assert [r["reason"] for r in rejected] == [reason]
    assert rejected[0]["transport"] == "stn"
