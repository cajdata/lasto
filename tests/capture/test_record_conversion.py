"""The safety core's frame types become plain records in one place (lasto.capture.convert)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lasto.capture.convert import to_record
from lasto.records import BusEvent, Frame
from lasto.safety.frames import CanFrame, ErrorFrame, ReadError, StatusMessage


def test_a_data_frame():
    record = to_record(CanFrame(0x025, bytes.fromhex("0FFF7F0000000000"), 1_234_567))
    assert record == Frame(hw_us=1_234_567, can_id=0x025, data=bytes.fromhex("0FFF7F0000000000"))


def test_extended_and_remote_frames_keep_their_flags():
    assert to_record(CanFrame(0x18DAF110, b"\x01", 5, extended=True)) == Frame(5, 0x18DAF110, b"\x01", extended=True)
    assert to_record(CanFrame(0x7E8, b"", 6, rtr=True)) == Frame(6, 0x7E8, b"", rtr=True)


def test_an_error_frame():
    record = to_record(ErrorFrame(0x04, b"\x01\x02", 99))
    assert record == Frame(hw_us=99, can_id=0x04, data=b"\x01\x02", error=True)


def test_bus_events():
    assert to_record(StatusMessage(0x00000008, 77)) == BusEvent("status", 0x00000008, 77)
    assert to_record(ReadError(0x00000020)) == BusEvent("read_error", 0x00000020, None)


def test_anything_else_is_refused():
    with pytest.raises(TypeError, match="str"):
        to_record("7E0#0201")  # type: ignore[arg-type]


@given(
    can_id=st.integers(0, 0x1FFFFFFF),
    data=st.binary(max_size=8),
    hw_us=st.integers(0, 2**63 - 1),
    extended=st.booleans(),
    rtr=st.booleans(),
)
def test_every_field_survives(can_id, data, hw_us, extended, rtr):
    record = to_record(CanFrame(can_id, data, hw_us, extended=extended, rtr=rtr))
    assert (record.can_id, record.data, record.hw_us, record.extended, record.rtr, record.error) == (
        can_id, data, hw_us, extended, rtr, False,
    )  # fmt: skip


def test_records_are_immutable():
    frame = Frame(1, 0x7E0, b"\x02")
    with pytest.raises(AttributeError):
        frame.can_id = 0x7E1  # type: ignore[misc]
