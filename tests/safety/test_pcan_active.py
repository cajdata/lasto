"""The polled (normal-mode) channel: the only CAN_Write binding."""

import pytest
from helpers import CHANNEL, HANDLE

from lasto.safety import pcan_constants as pc
from lasto.safety.errors import InterfaceError, SafetyViolation
from lasto.safety.pcan_active import TRANSMIT_FUNCTIONS, ActiveChannel, load_transmit, open_active
from lasto.sim.fake_pcan import FakePcanDll
from lasto.sim.pytest_plugin import HardwareFirewallError

REQUEST = bytes.fromhex("02010C0000000000")


def test_binds_the_read_only_set_plus_can_write():
    dll = FakePcanDll()
    load_transmit(dll)
    assert dll.looked_up == list(TRANSMIT_FUNCTIONS)
    assert TRANSMIT_FUNCTIONS[-1] == "CAN_Write"


def test_default_loader_is_blocked_in_tests():
    with pytest.raises(HardwareFirewallError):
        load_transmit()
    with pytest.raises(HardwareFirewallError):
        open_active(CHANNEL)


def test_opens_in_normal_mode_and_writes(sim):
    channel = open_active(CHANNEL, pcan=load_transmit(sim.dll))
    assert isinstance(channel, ActiveChannel)
    assert sim.dll.channel(HANDLE).listen_only_at_initialize == pc.PCAN_PARAMETER_OFF
    channel.write(0x7E0, REQUEST)
    assert sim.dll.writes == [(HANDLE, 0x7E0, REQUEST)]


@pytest.mark.parametrize(("can_id", "data"), [(0x800, REQUEST), (-1, REQUEST), (0x7E0, REQUEST[:7]), (0x7E0, REQUEST + b"\x00")])
def test_write_only_takes_8_byte_standard_frames(can_id, data):
    dll = FakePcanDll()
    channel = open_active(CHANNEL, pcan=load_transmit(dll))
    with pytest.raises(SafetyViolation):
        channel.write(can_id, data)
    assert dll.writes == []


def test_write_failure():
    dll = FakePcanDll()
    channel = open_active(CHANNEL, pcan=load_transmit(dll))
    dll.write_status = pc.PCAN_ERROR_XMTFULL
    with pytest.raises(InterfaceError, match="CAN_Write failed"):
        channel.write(0x7E0, REQUEST)


def test_enter_listen_only():
    dll = FakePcanDll()
    channel = open_active(CHANNEL, pcan=load_transmit(dll))
    assert channel.enter_listen_only()
    assert dll.channel(HANDLE).listen_only == pc.PCAN_PARAMETER_ON
    dll.readback_listen_only = pc.PCAN_PARAMETER_OFF
    assert not channel.enter_listen_only()
    dll.fail_set[pc.PCAN_LISTEN_ONLY] = pc.PCAN_ERROR_ILLOPERATION
    assert not channel.enter_listen_only()


def test_refused_when_listen_only_cannot_be_cleared():
    dll = FakePcanDll()
    dll.fail_set[pc.PCAN_LISTEN_ONLY] = pc.PCAN_ERROR_ILLPARAMVAL
    with pytest.raises(InterfaceError, match="could not clear listen-only"):
        open_active(CHANNEL, pcan=load_transmit(dll))


def test_initialize_failure():
    dll = FakePcanDll()
    dll.initialize_status = pc.PCAN_ERROR_ILLHW
    with pytest.raises(InterfaceError, match="could not initialize"):
        open_active(CHANNEL, pcan=load_transmit(dll))


@pytest.mark.parametrize("fault", ["readback_on", "readback_error", "reporting"])
def test_closed_if_it_does_not_come_up_cleanly(fault):
    dll = FakePcanDll()
    if fault == "readback_on":
        dll.readback_listen_only = pc.PCAN_PARAMETER_ON
    elif fault == "readback_error":
        dll.fail_get[pc.PCAN_LISTEN_ONLY] = pc.PCAN_ERROR_ILLHW
    else:
        dll.fail_set[pc.PCAN_ALLOW_ERROR_FRAMES] = pc.PCAN_ERROR_ILLPARAMVAL
    with pytest.raises(InterfaceError):
        open_active(CHANNEL, pcan=load_transmit(dll))
    assert not dll.channel(HANDLE).initialized


def test_driver_and_availability_checks_apply():
    dll = FakePcanDll(api_version="4.2.0.0")
    with pytest.raises(InterfaceError):
        open_active(CHANNEL, pcan=load_transmit(dll))
    dll = FakePcanDll()
    dll.channel(HANDLE).condition = pc.PCAN_CHANNEL_OCCUPIED
    with pytest.raises(InterfaceError):
        open_active(CHANNEL, pcan=load_transmit(dll))
