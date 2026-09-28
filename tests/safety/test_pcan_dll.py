"""The read-only PCAN binding: what it binds, what it refuses, and how it reads."""

import ctypes

import pytest
from helpers import HANDLE

from lasto.safety import pcan_constants as pc
from lasto.safety import pcan_dll
from lasto.safety.audit import REFUSALS, Auditor, MemoryAuditSink
from lasto.safety.errors import InterfaceError, SafetyViolation
from lasto.safety.frames import CanFrame, ErrorFrame, ReadError, StatusMessage
from lasto.safety.pcan_dll import PcanChannel, ReadOnlyPcan, load_readonly
from lasto.sim.fake_pcan import FakePcanDll
from lasto.sim.pytest_plugin import HardwareFirewallError


def test_binds_only_the_read_only_functions():
    dll = FakePcanDll()
    load_readonly(dll)
    assert dll.looked_up == list(pcan_dll.READONLY_FUNCTIONS)
    assert not any(name.startswith(("CAN_Write", "CAN_Reset", "CAN_Filter")) for name in dll.looked_up)


def test_setvalue_only_accepts_listed_settings():
    pcan = load_readonly(FakePcanDll())
    for parameter, value in [
        (pc.PCAN_LISTEN_ONLY, pc.PCAN_PARAMETER_OFF),
        (pc.PCAN_ALLOW_ERROR_FRAMES, pc.PCAN_PARAMETER_OFF),
        (0x07, 1),  # bus-off auto reset
        (0x01, 5),  # device ID: a write to the device
    ]:
        with pytest.raises(SafetyViolation) as refused:
            pcan.set_value(HANDLE, parameter, value)
        assert refused.value.reason == "pcan_setting_not_allowed"


def test_dll_path(monkeypatch):
    monkeypatch.setenv("SystemRoot", r"D:\Win")
    assert pcan_dll.dll_path() == r"D:\Win\System32\PCANBasic.dll"
    monkeypatch.delenv("SystemRoot")
    assert pcan_dll.dll_path() == r"C:\Windows\System32\PCANBasic.dll"


def test_loading_the_real_dll_is_blocked_by_the_test_firewall():
    with pytest.raises(HardwareFirewallError):
        pcan_dll.load_library()
    with pytest.raises(HardwareFirewallError):
        load_readonly()


def test_the_test_run_is_seen_as_firewalled():
    # The plugin marked its wrapper; test_killswitch.py shows a process without it isn't seen as firewalled.
    assert pcan_dll.hardware_firewall_installed()


def test_load_library_needs_64_bit_python():
    with pytest.raises(InterfaceError, match="64-bit"):
        pcan_dll.require_64_bit(4)
    pcan_dll.require_64_bit(8)
    assert pcan_dll.POINTER_BYTES == 8  # the process these tests run in


def test_load_library_reports_a_missing_dll(monkeypatch):
    def missing(path):
        raise OSError("not found")

    monkeypatch.setattr(ctypes, "WinDLL", missing)
    with pytest.raises(InterfaceError, match="could not load"):
        pcan_dll.load_library()


def test_load_library_loads_by_absolute_path(monkeypatch):
    seen = []
    monkeypatch.setattr(ctypes, "WinDLL", lambda path: seen.append(path) or "library")
    assert pcan_dll.load_library() == "library"
    assert seen == [pcan_dll.dll_path()]


def test_error_text():
    dll = FakePcanDll()
    pcan = load_readonly(dll)
    assert pcan.error_text(0x1400) == "0x1400 simulated PCAN error"
    dll.error_text_status = pc.PCAN_ERROR_ILLPARAMVAL
    assert pcan.error_text(0x1400) == "0x1400"


def message(msgtype, can_id=0x123, data=b"\x01\x02", length=None):
    msg = pc.TPCANMsg()
    msg.ID, msg.MSGTYPE, msg.LEN = can_id, msgtype, len(data) if length is None else length
    for i, byte in enumerate(data):
        msg.DATA[i] = byte
    return msg


def stamp(millis=5, overflow=0, micros=7):
    ts = pc.TPCANTimestamp()
    ts.millis, ts.millis_overflow, ts.micros = millis, overflow, micros
    return ts


def test_convert():
    assert pcan_dll.convert(message(pc.PCAN_MESSAGE_STANDARD), stamp()) == CanFrame(0x123, b"\x01\x02", 5007)
    extended = pcan_dll.convert(message(pc.PCAN_MESSAGE_EXTENDED | pc.PCAN_MESSAGE_RTR, 0x18DAF110), stamp())
    assert (extended.extended, extended.rtr) == (True, True)
    clipped = pcan_dll.convert(message(pc.PCAN_MESSAGE_STANDARD, data=bytes(8), length=15), stamp())
    assert clipped.data == bytes(8)
    assert pcan_dll.convert(message(pc.PCAN_MESSAGE_ERRFRAME, 2, b"\x01\x19"), stamp()) == ErrorFrame(2, b"\x01\x19", 5007)
    status = pcan_dll.convert(message(pc.PCAN_MESSAGE_STATUS, 0, b"\x00\x00\x00\x10", 4), stamp())
    assert status == StatusMessage(pc.PCAN_ERROR_BUSOFF, 5007)


def test_timestamp_overflow():
    assert pc.timestamp_us(stamp(millis=1, overflow=1, micros=2)) == 2 + 1000 + 0x100000000 * 1000


def test_check_driver():
    dll = FakePcanDll()
    pcan = load_readonly(dll)
    assert pcan_dll.check_driver(pcan) == "4.7.0.11"
    for version in ["4.6.9.1", "5.0.0.1", "garbage", "4.7"]:
        dll.api_version = version
        with pytest.raises(InterfaceError, match="isn't supported"):
            pcan_dll.check_driver(pcan)
    dll.api_version = "5.1.0.4"
    pcan_dll.check_driver(pcan)
    dll.fail_get[pc.PCAN_API_VERSION] = pc.PCAN_ERROR_NODRIVER
    with pytest.raises(InterfaceError, match="could not read"):
        pcan_dll.check_driver(pcan)


def test_check_available():
    dll = FakePcanDll()
    pcan = load_readonly(dll)
    pcan_dll.check_available(pcan, HANDLE, "PCAN_USBBUS1")
    for condition in (pc.PCAN_CHANNEL_OCCUPIED, pc.PCAN_CHANNEL_PCANVIEW, pc.PCAN_CHANNEL_UNAVAILABLE):
        dll.channel(HANDLE).condition = condition
        with pytest.raises(InterfaceError, match="isn't available"):
            pcan_dll.check_available(pcan, HANDLE, "PCAN_USBBUS1")
    dll.fail_get[pc.PCAN_CHANNEL_CONDITION] = pc.PCAN_ERROR_ILLHW
    with pytest.raises(InterfaceError, match="could not check"):
        pcan_dll.check_available(pcan, HANDLE, "PCAN_USBBUS1")


def open_channel(dll):
    pcan = load_readonly(dll)
    pcan.initialize(HANDLE, pc.PCAN_BAUD_500K)
    return PcanChannel(pcan, HANDLE, "PCAN_USBBUS1", "4.7.0.11")


def test_reading():
    dll = FakePcanDll()
    channel = open_channel(dll)
    assert channel.name == "PCAN_USBBUS1"
    assert channel.read_one() is None
    dll.inject_frame(HANDLE, 0x025, b"\x07\xff")
    dll.inject_status(HANDLE, pc.PCAN_ERROR_BUSHEAVY)
    dll.inject_read_error(HANDLE, pc.PCAN_ERROR_QOVERRUN)
    dll.inject_frame(HANDLE, 0x0B4, b"\x00")
    items = channel.drain()
    assert items == [
        CanFrame(0x025, b"\x07\xff", 0),
        StatusMessage(pc.PCAN_ERROR_BUSHEAVY, 0),
        ReadError(pc.PCAN_ERROR_QOVERRUN),
    ]  # stops at the read error
    assert channel.drain() == [CanFrame(0x0B4, b"\x00", 0)]


def test_drain_limit():
    dll = FakePcanDll()
    channel = open_channel(dll)
    for i in range(3):
        dll.inject_frame(HANDLE, 0x100 + i, b"")
    assert len(channel.drain(limit=2)) == 2
    assert len(channel.drain()) == 1


def test_status_listen_only_and_describe():
    dll = FakePcanDll()
    channel = open_channel(dll)
    assert channel.status() == pc.PCAN_ERROR_OK
    assert not channel.listen_only()
    dll.readback_listen_only = pc.PCAN_PARAMETER_ON
    assert channel.listen_only()
    dll.fail_get[pc.PCAN_LISTEN_ONLY] = pc.PCAN_ERROR_ILLHW
    assert not channel.listen_only()
    assert channel.describe() == {
        "channel": "PCAN_USBBUS1",
        "api_version": "4.7.0.11",
        "hardware": "PCAN-USB (simulated)",
        "channel_version": "simulated channel",
    }
    dll.fail_get[pc.PCAN_HARDWARE_NAME] = pc.PCAN_ERROR_ILLHW
    assert channel.describe()["hardware"] == "unknown"


def test_enable_reporting_failure():
    dll = FakePcanDll()
    channel = open_channel(dll)
    channel.enable_reporting()
    assert dll.channel(HANDLE).error_frames and dll.channel(HANDLE).status_frames
    dll.fail_set[pc.PCAN_ALLOW_STATUS_FRAMES] = pc.PCAN_ERROR_ILLPARAMVAL
    with pytest.raises(InterfaceError, match="error and status reporting"):
        channel.enable_reporting()


def test_close_is_idempotent():
    dll = FakePcanDll()
    channel = open_channel(dll)
    assert channel.close() is True
    assert channel.close() is True
    assert dll.calls.count("CAN_Uninitialize") == 1


def test_a_close_the_driver_refuses_is_audited_and_can_be_tried_again(clock):
    """Finding #5: CAN_Uninitialize's answer counts; a channel that didn't close may still be on the bus."""
    sink = MemoryAuditSink()
    REFUSALS.attach(Auditor(sink, clock))
    dll = FakePcanDll()
    channel = open_channel(dll)
    dll.uninitialize_status = pc.PCAN_ERROR_ILLOPERATION
    assert channel.close() is False
    assert dll.channel(HANDLE).initialized
    [failed] = [r for r in sink.records if r["event"] == "channel_close_failed"]
    assert failed["channel"] == "PCAN_USBBUS1" and "0x8000000" in failed["status"]
    dll.uninitialize_status = pc.PCAN_ERROR_OK
    assert channel.close() is True
    assert not dll.channel(HANDLE).initialized


def test_a_channel_already_gone_counts_as_closed():
    dll = FakePcanDll()
    channel = open_channel(dll)
    dll.uninitialize_status = pc.PCAN_ERROR_INITIALIZE  # the driver says it isn't initialized
    assert channel.close() is True


def test_channel_names():
    assert pc.channel_handle("PCAN_USBBUS1") == 0x51
    assert pc.channel_handle("PCAN_USBBUS8") == 0x58
    assert pc.channel_handle("PCAN_USBBUS9") == 0x509
    assert pc.channel_handle("PCAN_USBBUS16") == 0x510
    for bad in ["PCAN_USBBUS0", "PCAN_USBBUS17", "PCAN_PCIBUS1", "pcan_usbbus1", "PCAN_USBBUS1 ", None, 81]:
        with pytest.raises(ValueError):
            pc.channel_handle(bad)


def test_api_versions():
    assert pc.parse_api_version("4.7.0.123") == (4, 7, 0)
    assert pc.parse_api_version("PCAN-Basic 5.1") is None
    assert not pc.api_version_supported(None)


def test_readonly_pcan_is_constructed_from_functions():
    calls = []
    pcan = ReadOnlyPcan({"CAN_GetStatus": lambda handle: calls.append(handle.value) or -1})
    assert pcan.get_status(0x51) == 0xFFFFFFFF  # masked to an unsigned status
    assert calls == [0x51]
