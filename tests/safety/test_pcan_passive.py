"""Rule 1: passive mode is listen-only before the controller starts, confirmed by readback, or it refuses."""

import pytest
from helpers import CHANNEL, HANDLE

from lasto.safety import pcan_constants as pc
from lasto.safety import pcan_dll
from lasto.safety.audit import REFUSALS
from lasto.safety.errors import InterfaceError, PassiveModeUnconfirmed
from lasto.safety.pcan_dll import ReadOnlyPcan, load_readonly
from lasto.safety.pcan_passive import PassiveChannel, open_passive
from lasto.sim.fake_pcan import FakePcanDll
from lasto.sim.pytest_plugin import HardwareFirewallError


def test_listen_only_is_set_before_initialize_and_read_back():
    dll = FakePcanDll()
    channel = open_passive(CHANNEL, pcan=load_readonly(dll))
    assert isinstance(channel, PassiveChannel)
    state = dll.channel(HANDLE)
    assert state.listen_only_at_initialize == pc.PCAN_PARAMETER_ON  # never initialized active
    assert dll.calls[:2] == [f"CAN_SetValue 0x{pc.PCAN_LISTEN_ONLY:02X}=1", "CAN_Initialize"]
    assert channel.listen_only()
    assert state.error_frames and state.status_frames
    assert "CAN_Write" not in dll.looked_up


def test_passive_channels_have_no_way_to_write():
    channel = open_passive(CHANNEL, pcan=load_readonly(FakePcanDll()))
    assert not any("write" in name.lower() for name in dir(channel))
    assert not any("write" in name.lower() for name in dir(channel._pcan))


def test_default_loader_is_the_real_dll_which_tests_cannot_reach():
    with pytest.raises(HardwareFirewallError):
        open_passive(CHANNEL)


def test_bad_channel_name():
    with pytest.raises(ValueError):
        open_passive("PCAN_USBBUS99", pcan=load_readonly(FakePcanDll()))


def test_unsupported_driver_refused():
    dll = FakePcanDll(api_version="5.0.0.9")
    with pytest.raises(InterfaceError):
        open_passive(CHANNEL, pcan=load_readonly(dll))
    assert "CAN_Initialize" not in dll.calls


def test_channel_in_use_refused():
    dll = FakePcanDll()
    dll.channel(HANDLE).condition = pc.PCAN_CHANNEL_PCANVIEW
    with pytest.raises(InterfaceError):
        open_passive(CHANNEL, pcan=load_readonly(dll))
    assert "CAN_Initialize" not in dll.calls


def test_refuses_if_listen_only_cannot_be_set_before_initialize():
    dll = FakePcanDll()
    dll.fail_set[pc.PCAN_LISTEN_ONLY] = pc.PCAN_ERROR_ILLPARAMVAL
    with pytest.raises(PassiveModeUnconfirmed):
        open_passive(CHANNEL, pcan=load_readonly(dll))
    assert "CAN_Initialize" not in dll.calls  # no fallback that initializes first


def test_initialize_failure():
    dll = FakePcanDll()
    dll.initialize_status = pc.PCAN_ERROR_ILLHW
    with pytest.raises(InterfaceError, match="could not initialize"):
        open_passive(CHANNEL, pcan=load_readonly(dll))


def test_refuses_and_closes_if_readback_is_not_listen_only():
    dll = FakePcanDll()
    dll.readback_listen_only = pc.PCAN_PARAMETER_OFF
    with pytest.raises(PassiveModeUnconfirmed):
        open_passive(CHANNEL, pcan=load_readonly(dll))
    assert not dll.channel(HANDLE).initialized


def test_closes_if_error_reporting_cannot_be_enabled():
    dll = FakePcanDll()
    dll.fail_set[pc.PCAN_ALLOW_ERROR_FRAMES] = pc.PCAN_ERROR_ILLPARAMVAL
    with pytest.raises(InterfaceError):
        open_passive(CHANNEL, pcan=load_readonly(dll))
    assert not dll.channel(HANDLE).initialized


class ReadOnlyLookAlike:
    """Every read-only call a passive channel makes, plus a way to write: what #6 must keep out."""

    def __init__(self, dll):
        self._inner = load_readonly(dll)
        self.write = dll.CAN_Write

    def __getattr__(self, name):
        return getattr(self._inner, name)


class WiderBinding(ReadOnlyPcan):
    """A subclass could add anything; only the binding class itself is trusted."""

    __slots__ = ()


def _look_alike(dll):
    return ReadOnlyLookAlike(dll)


def _subclass(dll):
    return WiderBinding(pcan_dll.bind_readonly(dll))


@pytest.mark.parametrize("make", [_look_alike, _subclass])
def test_passive_path_takes_only_the_read_only_binding_itself(auditor, sink, make):
    """Finding #6: whatever is handed in, the passive path never gets a binding that could transmit."""
    REFUSALS.attach(auditor)
    dll = FakePcanDll()
    with pytest.raises(TypeError):
        open_passive(CHANNEL, pcan=make(dll))
    assert "CAN_Initialize" not in dll.calls
    assert [r["reason"] for r in sink.records if r["event"] == "rejected"] == ["passive_needs_the_read_only_binding"]


def test_passive_capture_never_writes(sim, clock):
    channel = open_passive(CHANNEL, pcan=load_readonly(sim.dll))
    clock.advance(1.0)
    frames = channel.drain()
    assert len(frames) > 100  # broadcast traffic is received
    assert sim.dll.writes == []
