"""The simulator catches forbidden traffic on its own, and fails the test run when it does."""

import ctypes

import pytest

from lasto.safety import pcan_constants as pc
from lasto.sim.bus import Broadcaster, SimBus
from lasto.sim.clock import FakeClock
from lasto.sim.ecu import MockEcu
from lasto.sim.fake_pcan import FakePcanDll
from lasto.sim.fake_stn import FakeStnPort
from lasto.sim.oracle import frame_violations
from lasto.sim.pytest_plugin import HardwareFirewallError
from lasto.sim.tester import SimTester
from lasto.sim.vehicle import ENGINE_RPM_ID, STEERING_ANGLE_ID, VEHICLE_SPEED_ID, build_sim
from lasto.sim.violations import VIOLATIONS, ViolationRecorder

GOOD = bytes.fromhex("02010C0000000000")


def check(can_id, data, *, listen_only=False, broadcast=(), awaiting=()):
    return frame_violations(can_id, data, listen_only=listen_only, broadcast_ids=broadcast, awaiting_flow_control=awaiting)


# ---- oracle ----


def test_oracle_accepts_allowed_frames():
    assert check(0x7E0, GOOD) == []
    assert check(0x7DF, GOOD) == []
    assert check(0x7E0, bytes.fromhex("0221D90000000000")) == []
    assert check(0x7E0, bytes.fromhex("3000000000000000"), awaiting={0x7E0}) == []


@pytest.mark.parametrize(
    ("kwargs", "text"),
    [
        ({"can_id": 0x7E0, "data": GOOD, "listen_only": True}, "listen-only"),
        ({"can_id": 0x025, "data": GOOD}, "non-diagnostic ID"),
        ({"can_id": 0x7E0, "data": GOOD, "broadcast": {0x7E0}}, "broadcast"),
        ({"can_id": 0x7E0, "data": GOOD[:7]}, "not 8 bytes"),
        ({"can_id": 0x7E0, "data": bytes.fromhex("0010000000000000")}, "malformed single frame"),
        ({"can_id": 0x7E0, "data": bytes.fromhex("0214010000000000")}, "denied service 0x14"),
        ({"can_id": 0x7E0, "data": bytes.fromhex("023E000000000000")}, "denied service 0x3E"),
        ({"can_id": 0x7DF, "data": bytes.fromhex("0221010000000000")}, "functional ID"),
        ({"can_id": 0x7E0, "data": bytes.fromhex("3000000000000000")}, "flow control no ECU asked for"),
        ({"can_id": 0x7E0, "data": bytes.fromhex("1008010203040506")}, "single frames and flow control"),
        ({"can_id": 0x7E0, "data": bytes.fromhex("2101020304050607")}, "single frames and flow control"),
        ({"can_id": 0x7E0, "data": b""}, "single frames and flow control"),
    ],
)
def test_oracle_flags(kwargs, text):
    found = check(kwargs.pop("can_id"), kwargs.pop("data"), **kwargs)
    assert any(text in item for item in found), found


# ---- fake DLL ----


def written_by_hand(dll, can_id, data, handle=0x51):
    message = pc.TPCANMsg()
    message.ID, message.LEN = can_id, len(data)
    for i, byte in enumerate(data):
        message.DATA[i] = byte
    return dll.CAN_Write(ctypes.c_uint16(handle), ctypes.pointer(message))


def initialize(dll, handle=0x51, listen_only=False):
    if listen_only:
        dll.CAN_SetValue(handle, pc.PCAN_LISTEN_ONLY, ctypes.pointer(ctypes.c_uint32(1)), 4)
    assert dll.CAN_Initialize(handle, pc.PCAN_BAUD_500K, 0, 0, 0) == pc.PCAN_ERROR_OK


def test_any_write_in_listen_only_is_a_violation():
    sim = build_sim()
    initialize(sim.dll, listen_only=True)
    with VIOLATIONS.expect() as caught:
        assert written_by_hand(sim.dll, 0x7E0, GOOD) == pc.PCAN_ERROR_ILLOPERATION
    assert caught and "listen-only" in caught[0]


def test_writes_on_broadcast_ids_and_denied_services_are_violations():
    sim = build_sim()
    initialize(sim.dll)
    with VIOLATIONS.expect() as caught:
        written_by_hand(sim.dll, STEERING_ANGLE_ID, GOOD)
        written_by_hand(sim.dll, 0x7E0, bytes.fromhex("0211010000000000"))  # ECU reset
    assert len(caught) == 3  # non-diagnostic, broadcast, denied service


def test_writes_before_initialize_are_violations():
    dll = FakePcanDll()
    with VIOLATIONS.expect() as caught:
        assert written_by_hand(dll, 0x7E0, GOOD) == pc.PCAN_ERROR_INITIALIZE
    assert caught


def test_fake_dll_driver_behavior():
    dll = FakePcanDll()
    handle = 0x51
    assert dll.CAN_Uninitialize(handle) == pc.PCAN_ERROR_INITIALIZE
    assert dll.CAN_GetStatus(handle) == pc.PCAN_ERROR_INITIALIZE
    assert dll.CAN_Read(handle, None, None) == pc.PCAN_ERROR_INITIALIZE
    value = ctypes.c_uint32(1)
    assert dll.CAN_SetValue(handle, pc.PCAN_ALLOW_ERROR_FRAMES, ctypes.pointer(value), 4) == pc.PCAN_ERROR_INITIALIZE
    assert dll.CAN_SetValue(handle, 0x07, ctypes.pointer(value), 4) == pc.PCAN_ERROR_ILLPARAMTYPE
    assert dll.CAN_GetValue(handle, 0x07, ctypes.pointer(value), 4) == pc.PCAN_ERROR_ILLPARAMTYPE
    initialize(dll)
    assert dll.CAN_Initialize(handle, pc.PCAN_BAUD_500K, 0, 0, 0) == pc.PCAN_ERROR_ILLOPERATION
    dll.channel(handle).status = pc.PCAN_ERROR_BUSHEAVY
    assert dll.CAN_GetStatus(handle) == pc.PCAN_ERROR_BUSHEAVY
    dll.write_status = pc.PCAN_ERROR_XMTFULL
    assert written_by_hand(dll, 0x7E0, GOOD) == pc.PCAN_ERROR_XMTFULL
    dll.write_status = pc.PCAN_ERROR_OK
    assert written_by_hand(dll, 0x7E0, GOOD) == pc.PCAN_ERROR_OK  # no bus attached
    dll.channel(handle).unplugged = True
    assert dll.CAN_GetStatus(handle) == pc.PCAN_ERROR_ILLHW
    assert dll.CAN_Read(handle, None, None) == pc.PCAN_ERROR_ILLHW
    dll.channel(0x52).unplugged = True
    assert dll.CAN_Initialize(0x52, pc.PCAN_BAUD_500K, 0, 0, 0) == pc.PCAN_ERROR_ILLHW


def test_read_timestamps_carry_into_the_overflow_word():
    dll = FakePcanDll()
    initialize(dll)
    dll.channel(0x51).rx.append((0, 0, 0x100, b"\x01", 5_000_000.000123))
    message, stamp = pc.TPCANMsg(), pc.TPCANTimestamp()
    dll.CAN_Read(0x51, ctypes.pointer(message), ctypes.pointer(stamp))
    assert pc.timestamp_us(stamp) == 5_000_000_000_123
    assert stamp.millis_overflow == 1


# ---- bus, ECUs, tester ----


def test_bus_orders_broadcasts_and_scheduled_frames():
    clock = FakeClock()
    bus = SimBus(clock)
    seen = []
    bus.add_tap("tap", lambda time, can_id, data: seen.append((round(time - 1000, 3), can_id)))
    bus.add_broadcaster(Broadcaster(0x100, 0.010, lambda t: b"\x00"))
    bus.schedule(0.015, 0x200, b"\x01", "someone")
    clock.advance(0.025)
    bus.advance()
    assert seen == [(0.0, 0x100), (0.01, 0x100), (0.015, 0x200), (0.02, 0x100)]
    assert bus.broadcast_ids == {0x100}
    bus.remove_tap("tap")
    bus.remove_tap("tap")
    clock.advance(0.1)
    bus.advance()
    assert len(seen) == 4


def test_vehicle_broadcasts():
    sim = build_sim()
    assert {STEERING_ANGLE_ID, VEHICLE_SPEED_ID, ENGINE_RPM_ID} <= sim.bus.broadcast_ids
    steering = sim.vehicle._steering(0.0)
    raw = (steering[0] << 8) | steering[1]
    assert raw * 1.125 - 1152 == 1150.875  # the stuck value the truck reports
    sim.vehicle.state.speed_kph = 88
    assert int.from_bytes(sim.vehicle._speed(0.0)[5:7], "big") == 8800
    sim.vehicle.state.rpm = 2000
    assert int.from_bytes(sim.vehicle._rpm(0.0)[:2], "big") == 2000


def test_mock_ecu_answers_and_misbehaves():
    clock = FakeClock()
    bus = SimBus(clock)
    ecu = MockEcu("ecm", request_id=0x7E0, response_id=0x7E8, pids={0x0D: b"\x00"}, local_ids={0x01: b"\xaa"}, dtcs=[0x0420, 0x0171])
    bus.add_node(ecu)
    out = []
    bus.add_tap("interface", lambda t, can_id, data: out.append((can_id, data[: 1 + (data[0] & 0x0F)] if data[0] >> 4 == 0 else data)))

    def ask(can_id, payload):
        out.clear()
        SimTester(bus).request(can_id, payload)
        clock.advance(0.05)
        bus.advance()
        return [data[1:] for cid, data in out if cid == 0x7E8]

    assert ask(0x7E0, b"\x01\x0d\x0c") == [b"\x41\x0d\x00"]
    assert ask(0x7E0, b"\x01\x0c") == []  # unsupported PIDs get no answer
    assert ask(0x7DF, b"\x03") == [bytes.fromhex("43020420 0171".replace(" ", ""))]
    assert ask(0x7E0, b"\x21\x01") == [b"\x61\x01\xaa"]
    assert ask(0x7E0, b"\x21\x02") == [b"\x7f\x21\x31"]
    assert ask(0x7E0, b"\x22\xf1\x90") == [b"\x7f\x22\x31"]
    assert ask(0x7E0, b"\x1a\x88") == [b"\x5a\x88ECM"]
    assert ask(0x7E0, b"\x2e\x01") == [b"\x7f\x2e\x11"]
    assert ask(0x7DF, b"\x21\x01") == []  # manufacturer services never answer functionally
    assert ask(0x7DF, b"\x09\x04") == []
    ecu.script.extend(["silent", "busy", 0x22, b"\x41\x00"])
    assert ask(0x7E0, b"\x01\x0d") == []
    assert ask(0x7E0, b"\x01\x0d") == [b"\x7f\x01\x21"]
    assert ask(0x7E0, b"\x01\x0d") == [b"\x7f\x01\x22"]
    assert ask(0x7E0, b"\x01\x0d") == [b"\x41\x00"]
    ecu.on_frame(bus, 0.0, 0x7E0, b"")
    ecu.on_frame(bus, 0.0, 0x7E0, bytes(8))  # empty single frame
    ecu.on_frame(bus, 0.0, 0x7E1, GOOD)  # another ECU's request


def test_mock_ecu_waits_for_flow_control():
    clock = FakeClock()
    bus = SimBus(clock)
    ecu = MockEcu("engine", request_id=0x7E0, response_id=0x7E8)
    bus.add_node(ecu)
    frames = []
    bus.add_tap("interface", lambda t, can_id, data: frames.append(data))
    SimTester(bus).request(0x7E0, b"\x09\x02")
    clock.advance(0.01)
    bus.advance()
    assert frames[-1][:2] == b"\x10\x14" and ecu.awaiting_flow_control
    assert bus.awaiting_flow_control() == {0x7E0}
    bus.transmit(0x7E0, bytes.fromhex("3000000000000000"), "interface")
    clock.advance(0.01)
    bus.advance()
    assert [f[0] for f in frames[-2:]] == [0x21, 0x22]  # 14 bytes after the first frame
    assert not ecu.awaiting_flow_control


def test_pending_behavior_answers_later():
    sim = build_sim()
    sim.vehicle.engine.script.append("pending")
    sim.vehicle.engine.script.append("pending")
    frames = []
    sim.bus.add_tap("interface", lambda t, can_id, data: frames.append((can_id, data)) if can_id == 0x7E8 else None)
    SimTester(sim.bus).request(0x7E0, b"\x01\x0d")
    SimTester(sim.bus).request(0x7E0, b"\x01\x05")
    sim.clock.advance(1.0)
    sim.bus.advance()
    payloads = [data[1 : 1 + data[0]] for _, data in frames]
    assert payloads == [b"\x7f\x01\x78", b"\x7f\x01\x78", b"\x41\x0d\x00", b"\x41\x05\x7d"]


# ---- fake STN adapter ----


def feed(port, text):
    port.write(text.encode("ascii"))
    return port.read_until(b">")


@pytest.mark.parametrize(
    ("line", "text"),
    [
        ("\r", "bare carriage return"),
        ("0100\r", "hex line"),
        ("STCMM1\r", "dangerous"),
        ("ATPP21SV00\r", "dangerous"),
        ("STPX H:7E0\r", "unexpected byte"),
        ("ATSP0\r", "dangerous"),
        ("STP22\r", "initializes the bus"),
        ("ATXYZ\r", "doesn't model"),
        ("STMA\r", "automatic protocol search"),
    ],
)
def test_fake_adapter_flags_dangerous_input(line, text):
    port = FakeStnPort()
    with VIOLATIONS.expect() as caught:
        port.write(line.encode("latin-1"))
    assert any(text in item for item in caught), caught


def test_fake_adapter_flags_monitoring_that_would_ack_or_keep_alive():
    port = FakeStnPort(pp21=(0x00, True))
    feed(port, "STP31\r")
    with VIOLATIONS.expect() as caught:
        port.write(b"STMA\r")
    assert any("ACKs" in item for item in caught)
    port = FakeStnPort()
    feed(port, "STP23\r")
    with VIOLATIONS.expect() as caught:
        port.write(b"STMA\r")
    assert any("keep-alives" in item for item in caught)


def test_fake_adapter_flags_writes_during_the_bootloader_window():
    port = FakeStnPort()
    port.write(b"ATZ\r")
    with VIOLATIONS.expect() as caught:
        port.write(b"STI\r")
    assert any("bootloader" in item for item in caught)


def test_fake_adapter_parser_rules():
    port = FakeStnPort()
    assert b"OK" in feed(port, "at e0\r")  # spaces and case
    assert feed(port, "STX\x08I\r") == b"STN2255 v5.10.3\r\r>"  # backspace edits the line
    port.write(b"\x08")  # backspace on an empty line does nothing
    assert feed(port, "STVR2\r") == b"12.63\r\r>"
    assert feed(port, "STVRX\r") == b"0x7A3\r\r>"
    assert feed(port, "ATRV\r") == b"12.6V\r\r>"
    assert feed(port, "STPR\r") == b"0\r\r>"
    assert b"21:FF F" in feed(port, "ATPPS\r")
    port.voltage = "--.--"
    assert feed(port, "STVR1\r") == b"--.--\r\r>"
    port.reset_input_buffer()
    port.close()
    assert port.closed


def test_recorder_expect_keeps_earlier_violations():
    recorder = ViolationRecorder()
    recorder.record("earlier")
    with recorder.expect() as caught:
        recorder.record("inside")
    assert caught == ["inside"]
    assert recorder.take() == ["earlier"]


# ---- firewall and the plugin itself ----


def test_firewall_blocks_the_pcan_dll_and_serial_ports():
    with pytest.raises(HardwareFirewallError):
        ctypes.WinDLL("PCANBasic.dll")
    with pytest.raises(HardwareFirewallError):
        ctypes.windll.LoadLibrary(r"C:\Windows\System32\PCANBasic.dll")
    import serial

    with pytest.raises(HardwareFirewallError):
        serial.Serial("COM5")
    assert ctypes.WinDLL("kernel32") is not None  # other libraries still load


PLUGIN_ARGS = ["-p", "lasto.sim.pytest_plugin", "-p", "no:cacheprovider"]


def test_plugin_fails_a_test_that_sends_forbidden_traffic(pytester):
    pytester.makepyfile(
        """
        from lasto.sim.violations import VIOLATIONS

        def test_sends_something_forbidden():
            try:
                VIOLATIONS.record("denied service 0x11")
                raise RuntimeError("the code under test swallowed this")
            except RuntimeError:
                pass
        """
    )
    result = pytester.runpytest_subprocess(*PLUGIN_ARGS)
    # The test body "passes" because the code under test swallowed the error; the plugin fails it anyway.
    result.assert_outcomes(passed=1, errors=1)
    assert result.ret == 1
    result.stdout.fnmatch_lines(["*forbidden traffic*", "*denied service 0x11*"])


def test_plugin_fails_the_run_for_violations_outside_any_test(pytester):
    pytester.makepyfile(
        """
        import pytest
        from lasto.sim.violations import VIOLATIONS

        @pytest.fixture(scope="session")
        def late():
            yield
            VIOLATIONS.record("frame written while the channel is listen-only")

        def test_ok(late):
            pass
        """
    )
    result = pytester.runpytest_subprocess(*PLUGIN_ARGS)
    assert result.ret == 1
    result.stdout.fnmatch_lines(["*violations outside any test*"])


def test_plugin_installs_the_firewall_in_a_fresh_run(pytester):
    pytester.makepyfile(
        """
        import ctypes, pytest
        from lasto.sim.pytest_plugin import HardwareFirewallError

        def test_blocked():
            with pytest.raises(HardwareFirewallError):
                ctypes.WinDLL("PCANBasic")
        """
    )
    pytester.runpytest_subprocess(*PLUGIN_ARGS).assert_outcomes(passed=1)
