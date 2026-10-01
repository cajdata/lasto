"""Each second's per-ID rollup: frames, timing, DLC, and which bits changed (lasto.capture.rollups)."""

from __future__ import annotations

from lasto.capture.rollups import LastSeen, summarize
from lasto.records import Frame


def by_id(rows):
    return {(row.can_id, row.extended): row for row in rows}


def test_one_id_in_one_second():
    last: dict[tuple[int, bool], LastSeen] = {}
    frames = [
        Frame(1_000_000, 0x025, bytes.fromhex("0FFF000000000000")),
        Frame(1_010_000, 0x025, bytes.fromhex("0FFE000000000001")),
        Frame(1_030_000, 0x025, bytes.fromhex("0FFE000000000001")),
    ]
    [row] = summarize(frames, last)
    assert (row.can_id, row.extended, row.frames) == (0x025, False, 3)
    assert (row.first_hw_us, row.last_hw_us) == (1_000_000, 1_030_000)
    assert (row.gap_min_us, row.gap_max_us) == (10_000, 20_000)
    assert (row.dlc_min, row.dlc_max) == (8, 8)
    assert row.changed_bits == bytes.fromhex("0001000000000001")  # byte 1 bit 0, and byte 7 bit 0
    assert row.last_data == bytes.fromhex("0FFE000000000001")
    assert last[(0x025, False)] == LastSeen(1_030_000, bytes.fromhex("0FFE000000000001"))


def test_changes_and_gaps_count_across_the_second_boundary():
    last = {(0x0B4, False): LastSeen(990_000, bytes.fromhex("0000000000FF0000"))}
    [row] = summarize([Frame(1_010_000, 0x0B4, bytes.fromhex("0000000000FE0000"))], last)
    assert (row.gap_min_us, row.gap_max_us) == (20_000, 20_000)
    assert row.changed_bits == bytes.fromhex("0000000000010000")


def test_a_first_frame_has_no_gap_and_no_change():
    [row] = summarize([Frame(5, 0x2C4, b"\x01\x02")], {})
    assert (row.gap_min_us, row.gap_max_us, row.changed_bits) == (None, None, bytes(8))


def test_a_shorter_frame_counts_its_missing_bytes_as_changed():
    last = {(0x100, False): LastSeen(0, b"\x01\x02\x03")}
    [row] = summarize([Frame(10, 0x100, b"\x01")], last)
    assert row.changed_bits == bytes.fromhex("0002030000000000") and (row.dlc_min, row.dlc_max) == (1, 1)


def test_ids_and_extended_ids_are_kept_apart_and_error_frames_are_left_out():
    frames = [
        Frame(0, 0x7E0, b"\x01"),
        Frame(1, 0x7E0, b"\x01", extended=True),
        Frame(2, 0x04, b"\x01\x19", error=True),
        Frame(3, 0x7E0, b"\x02"),
    ]
    rows = by_id(summarize(frames, {}))
    assert set(rows) == {(0x7E0, False), (0x7E0, True)}
    assert rows[(0x7E0, False)].frames == 2 and rows[(0x7E0, True)].frames == 1


def test_remote_frames_count_with_no_data():
    [row] = summarize([Frame(0, 0x7E8, b"", rtr=True), Frame(7, 0x7E8, b"", rtr=True)], {})
    assert (row.frames, row.dlc_min, row.dlc_max, row.gap_min_us) == (2, 0, 0, 7)
