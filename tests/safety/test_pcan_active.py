"""The polled (normal-mode) channel and the write function: the only CAN_Write binding."""

import pytest
from helpers import CHANNEL, HANDLE, events

from lasto.safety import pcan_constants as pc
from lasto.safety import pcan_dll
from lasto.safety.audit import Auditor
from lasto.safety.errors import InterfaceError, KillSwitchTripped, SafetyViolation
from lasto.safety.killswitch import KILL_SWITCH
from lasto.safety.pcan_active import ActiveChannel, Writer, open_active
from lasto.sim.clock import FakeClock
from lasto.sim.fake_pcan import FakePcanDll
from lasto.sim.pytest_plugin import HardwareFirewallError

REQUEST = bytes.fromhex("02010C0000000000")


def opened(library, auditor, **options):
    options.setdefault("clock", FakeClock())
    return open_active(CHANNEL, library=library, auditor=auditor, **options)


def test_binds_the_read_only_set_plus_can_write(auditor):
    dll = FakePcanDll()
    opened(dll, auditor)
    assert dll.looked_up == [*pcan_dll.READONLY_FUNCTIONS, "CAN_Write"]


def test_the_read_only_binding_follows_its_list():
    assert list(pcan_dll.bind_readonly(FakePcanDll())) == list(pcan_dll.READONLY_FUNCTIONS)


def test_default_loader_is_blocked_in_tests(auditor):
    with pytest.raises(HardwareFirewallError):
        open_active(CHANNEL, auditor=auditor, clock=FakeClock())


def test_opens_in_normal_mode_and_writes_through_the_writer(sim, auditor, sink):
    channel, writer = opened(sim.dll, auditor)
    assert isinstance(channel, ActiveChannel) and isinstance(writer, Writer)
    assert sim.dll.channel(HANDLE).listen_only_at_initialize == pc.PCAN_PARAMETER_OFF
    listen_only_off = f"CAN_SetValue 0x{pc.PCAN_LISTEN_ONLY:02X}={pc.PCAN_PARAMETER_OFF}"
    assert sim.dll.calls[:2] == [listen_only_off, "CAN_Initialize"]  # cleared before initializing
    writer(0x7E0, REQUEST, purpose="test", kind="request")
    assert sim.dll.writes == [(HANDLE, 0x7E0, REQUEST)]
    [record] = events(sink, "transmit")
    assert (record["can_id"], record["data"], record["kind"]) == ("0x7E0", "02 01 0C 00 00 00 00 00", "request")


def test_the_channel_itself_cannot_write(auditor):
    channel, _writer = opened(FakePcanDll(), auditor)
    assert not any("write" in name.lower() for name in dir(channel))


def test_the_writer_refuses_broadcast_ids(auditor):
    dll = FakePcanDll()
    _channel, writer = opened(dll, auditor, broadcast_ids={0x7E0})
    with pytest.raises(SafetyViolation) as refused:
        writer(0x7E0, REQUEST, purpose="test", kind="request")
    assert refused.value.reason == "can_id_carries_broadcast"
    assert dll.writes == []


def test_the_writer_writes_nothing_if_the_audit_log_fails(auditor):
    class BrokenSink:
        def write(self, record):
            raise OSError("disk full")

    dll = FakePcanDll()
    _channel, writer = opened(dll, Auditor(BrokenSink(), FakeClock()))
    with pytest.raises(OSError):
        writer(0x7E0, REQUEST, purpose="test", kind="request")
    assert dll.writes == []


def test_the_writer_checks_the_kill_switch(auditor):
    dll = FakePcanDll()
    _channel, writer = opened(dll, auditor)
    KILL_SWITCH.trip("hotkey")
    with pytest.raises(KillSwitchTripped):
        writer(0x7E0, REQUEST, purpose="test", kind="request")
    assert dll.writes == []


def test_write_failure(auditor):
    dll = FakePcanDll()
    _channel, writer = opened(dll, auditor)
    dll.write_status = pc.PCAN_ERROR_XMTFULL
    with pytest.raises(InterfaceError, match="CAN_Write failed"):
        writer(0x7E0, REQUEST, purpose="test", kind="request")


def test_enter_listen_only(auditor):
    dll = FakePcanDll()
    channel, _writer = opened(dll, auditor)
    assert channel.enter_listen_only()
    assert dll.channel(HANDLE).listen_only == pc.PCAN_PARAMETER_ON
    dll.readback_listen_only = pc.PCAN_PARAMETER_OFF
    assert not channel.enter_listen_only()
    dll.fail_set[pc.PCAN_LISTEN_ONLY] = pc.PCAN_ERROR_ILLOPERATION
    assert not channel.enter_listen_only()


def test_refused_when_listen_only_cannot_be_cleared(auditor):
    dll = FakePcanDll()
    dll.fail_set[pc.PCAN_LISTEN_ONLY] = pc.PCAN_ERROR_ILLPARAMVAL
    with pytest.raises(InterfaceError, match="could not clear listen-only"):
        opened(dll, auditor)


def test_initialize_failure(auditor):
    dll = FakePcanDll()
    dll.initialize_status = pc.PCAN_ERROR_ILLHW
    with pytest.raises(InterfaceError, match="could not initialize"):
        opened(dll, auditor)


@pytest.mark.parametrize("fault", ["readback_on", "readback_error", "reporting"])
def test_closed_if_it_does_not_come_up_cleanly(auditor, fault):
    dll = FakePcanDll()
    if fault == "readback_on":
        dll.readback_listen_only = pc.PCAN_PARAMETER_ON
    elif fault == "readback_error":
        dll.fail_get[pc.PCAN_LISTEN_ONLY] = pc.PCAN_ERROR_ILLHW
    else:
        dll.fail_set[pc.PCAN_ALLOW_ERROR_FRAMES] = pc.PCAN_ERROR_ILLPARAMVAL
    with pytest.raises(InterfaceError):
        opened(dll, auditor)
    assert not dll.channel(HANDLE).initialized
    assert "CAN_Write" not in dll.looked_up  # never bound for a channel that didn't come up


def test_driver_and_availability_checks_apply(auditor):
    dll = FakePcanDll(api_version="4.2.0.0")
    with pytest.raises(InterfaceError):
        opened(dll, auditor)
    dll = FakePcanDll()
    dll.channel(HANDLE).condition = pc.PCAN_CHANNEL_OCCUPIED
    with pytest.raises(InterfaceError):
        opened(dll, auditor)
