import pytest

from lasto.safety import isotp
from lasto.safety.errors import SafetyViolation
from lasto.safety.isotp import FrameKind


def test_single_frame_normal_addressing():
    assert isotp.encode_single_frame(b"\x01\x0c", ext_address=None, padding=0xAA) == bytes.fromhex("02010CAAAAAAAAAA")
    assert isotp.encode_single_frame(bytes(range(1, 8)), ext_address=None, padding=0) == bytes.fromhex("0701020304050607")


def test_single_frame_extended_addressing():
    assert isotp.encode_single_frame(b"\x21\x01", ext_address=0x40, padding=0) == bytes.fromhex("4002210100000000")


@pytest.mark.parametrize(("payload", "ext"), [(b"", None), (bytes(8), None), (bytes(7), 0x40)])
def test_requests_that_need_more_than_one_frame_are_refused(payload, ext):
    with pytest.raises(SafetyViolation) as refused:
        isotp.encode_single_frame(payload, ext_address=ext, padding=0)
    assert refused.value.reason == "not_single_frame"


def test_flow_control():
    assert isotp.encode_flow_control(ext_address=None, padding=0) == bytes.fromhex("3000000000000000")
    assert isotp.encode_flow_control(ext_address=0x40, padding=0x55) == bytes.fromhex("4030000055555555")


def test_parse_every_frame_kind():
    single = isotp.parse(bytes.fromhex("0341 0C1A F8000000"), ext_address=None)
    assert (single.kind, single.payload) == (FrameKind.SINGLE, bytes.fromhex("410C1A"))
    first = isotp.parse(bytes.fromhex("1014490201414243"), ext_address=None)
    assert (first.kind, first.length, first.payload) == (FrameKind.FIRST, 20, bytes.fromhex("490201414243"))
    consecutive = isotp.parse(bytes.fromhex("2144454647484950"), ext_address=None)
    assert (consecutive.kind, consecutive.sequence) == (FrameKind.CONSECUTIVE, 1)
    assert consecutive.payload == bytes.fromhex("44454647484950")
    flow = isotp.parse(bytes.fromhex("3102050000000000"), ext_address=None)
    assert (flow.kind, flow.flow_status, flow.block_size, flow.st_min) == (FrameKind.FLOW_CONTROL, 1, 2, 5)


def test_parse_extended_addressing():
    frame = isotp.parse(bytes.fromhex("4002610100000000"), ext_address=0x40)
    assert (frame.kind, frame.payload) == (FrameKind.SINGLE, b"\x61\x01")
    first = isotp.parse(bytes.fromhex("4010076101020304"), ext_address=0x40)
    assert (first.kind, first.length) == (FrameKind.FIRST, 7)


@pytest.mark.parametrize(
    ("data", "ext", "reason"),
    [
        (b"", 0x40, "isotp_address_mismatch"),
        (bytes.fromhex("4102610100000000"), 0x40, "isotp_address_mismatch"),
        (b"", None, "isotp_malformed"),
        (b"\x40", 0x40, "isotp_malformed"),
        (bytes.fromhex("0000000000000000"), None, "isotp_malformed"),  # length 0
        (bytes.fromhex("0801020304050607"), None, "isotp_malformed"),  # length 8 in 8 bytes
        (b"\x02\x01", None, "isotp_malformed"),  # claims 2 bytes, has 1
        (b"\x10", None, "isotp_malformed"),  # first frame without a length byte
        (bytes.fromhex("1007010203040506"), None, "isotp_malformed"),  # first frame that fits one frame
        (bytes.fromhex("4010066101020304"), 0x40, "isotp_malformed"),
        (b"\x30\x00", None, "isotp_malformed"),
        (bytes.fromhex("4000000000000000"), None, "isotp_malformed"),  # frame type 4
        (bytes.fromhex("F000000000000000"), None, "isotp_malformed"),
    ],
)
def test_malformed_frames(data, ext, reason):
    with pytest.raises(SafetyViolation) as refused:
        isotp.parse(data, ext_address=ext)
    assert refused.value.reason == reason
