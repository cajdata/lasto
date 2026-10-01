"""Raw capture segments: candump text, one checksummed zstd frame per second (lasto.storage.segments)."""

from __future__ import annotations

import io
import os

import pytest
import zstandard
from hypothesis import given
from hypothesis import strategies as st

from lasto.records import Frame
from lasto.storage.segments import (
    SegmentDamaged,
    SegmentFile,
    TimeBase,
    candump_line,
    parse_candump_line,
    read_second_at,
    read_segment,
)

BASE = TimeBase(hw_us=1_000_000_000, utc_us=1_759_168_800_000_000)  # 2025-09-29T18:00:00Z at hw 1000 s


def second(start_us: int, count: int = 3) -> list[Frame]:
    return [Frame(start_us + 1000 * i, 0x025, bytes([i, 0xFF, 0x7F, 0, 0, 0, 0, i])) for i in range(count)]


def write(path, seconds: list[list[Frame]]) -> list[tuple[int, int]]:
    segment = SegmentFile(path, BASE)
    places = [segment.write_second(seq, frames) for seq, frames in enumerate(seconds)]
    segment.close()
    return places


def test_candump_lines_in_the_can_utils_format():
    assert candump_line(Frame(1_000_000_000, 0x7E0, bytes.fromhex("0201000000000000")), BASE) == (
        "(1759168800.000000) can0 7E0#0201000000000000\n"
    )
    assert candump_line(Frame(1_000_123_456, 0x18DAF110, b"\x02\x10", extended=True), BASE) == (
        "(1759168800.123456) can0 18DAF110#0210\n"
    )
    assert candump_line(Frame(1_000_000_001, 0x7E8, b"", rtr=True), BASE) == "(1759168800.000001) can0 7E8#R\n"
    # An error frame carries CAN_ERR_FLAG; the rest of the ID is PCAN's error type, not SocketCAN's class bits.
    assert candump_line(Frame(1_000_000_002, 0x04, b"\x01\x19", error=True), BASE) == (
        "(1759168800.000002) can0 20000004#0119\n"
    )


def test_timestamps_convert_exactly_both_ways():
    """Integer microseconds throughout: a float epoch with six decimals would lose the last digit."""
    for hw_us in (1_000_000_000, 1_000_000_001, 9_999_999_999_999):
        assert BASE.hw_us_of(BASE.utc_us_of(hw_us)) == hw_us
    frame = Frame(1_000_000_007, 0x0B4, b"\x00\x01", extended=False)
    assert parse_candump_line(candump_line(frame, BASE), BASE) == frame


@pytest.mark.parametrize(
    "line",
    [
        "(1759168800.000000) can0 7E0#02010",  # an odd number of hex digits
        "(1759168800.000000) can0 7E00#02",  # a four-digit ID
        "1759168800.000000 can0 7E0#02",
        "(1759168800.000000) can0 7E0#0102030405060708090A0B0C0D0E0F1011",  # more than 8 bytes
        "(1759168800.000000) can0 800007E0#00",  # a flag bit outside the ID and error bits: neither kind of frame
        "(1759168800.000000) can0 FFF#00",  # a standard ID above 0x7FF
    ],
)
def test_malformed_lines_are_refused(line):
    with pytest.raises(SegmentDamaged):
        parse_candump_line(line + "\n", BASE)


def test_a_segment_round_trips(tmp_path):
    seconds = [second(1_000_000_000), second(1_001_000_000, 5), [Frame(1_002_500_000, 0x04, b"\x01", error=True)]]
    places = write(tmp_path / "seg.candump.zst", seconds)
    read, good_length = read_segment(tmp_path / "seg.candump.zst", BASE)
    assert [block.frames for block in read] == [tuple(frames) for frames in seconds]
    assert [(block.seq, block.first_hw_us, block.last_hw_us) for block in read] == [
        (0, 1_000_000_000, 1_000_002_000),
        (1, 1_001_000_000, 1_001_004_000),
        (2, 1_002_500_000, 1_002_500_000),
    ]
    assert [(block.offset, block.length) for block in read] == places
    assert good_length == (tmp_path / "seg.candump.zst").stat().st_size


def test_every_kind_of_frame_round_trips(tmp_path):
    frames = [
        Frame(1_000_000_000, 0x025, b"\x0f\xff"),
        Frame(1_000_000_100, 0x18DAF110, b"\x02\x10\x03", extended=True),
        Frame(1_000_000_200, 0x7E8, b"", rtr=True),
        Frame(1_000_000_300, 0x18DAF110, b"", extended=True, rtr=True),
        Frame(1_000_000_400, 0x04, b"\x01\x19\x08\x00", error=True),
    ]
    write(tmp_path / "seg.candump.zst", [frames])
    [block], _ = read_segment(tmp_path / "seg.candump.zst", BASE)
    assert block.frames == tuple(frames)


def test_bytes_that_are_not_a_second_end_the_readable_part(tmp_path):
    places = write(tmp_path / "seg.candump.zst", [second(1_000_000_000)])
    with open(tmp_path / "seg.candump.zst", "ab") as file:
        file.write(b"not a second of candump")
    read, good_length = read_segment(tmp_path / "seg.candump.zst", BASE)
    assert len(read) == 1 and good_length == places[0][1]


def test_a_place_with_the_wrong_length_is_refused(tmp_path):
    [(offset, length)] = write(tmp_path / "seg.candump.zst", [second(1_000_000_000)])
    with pytest.raises(SegmentDamaged, match="recorded length"):
        read_second_at(tmp_path / "seg.candump.zst", offset, length + 5, BASE)


def test_a_segment_over_a_stream_leaves_it_open():
    buffer = io.BytesIO()
    segment = SegmentFile(buffer, BASE)
    segment.write_second(0, second(1_000_000_000))
    segment.close()
    assert not buffer.closed


class FailingStream(io.BytesIO):
    """A segment's file whose writes fail on demand: partway through a block, or taking nothing at all."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_writes = 0
        self.takes_nothing = False
        self.cut_fails = False

    def write(self, data) -> int:
        if self.takes_nothing:
            return 0
        if self.fail_writes:
            self.fail_writes -= 1
            super().write(bytes(data[: len(data) // 2]))  # half the block reaches the file
            raise OSError(28, "No space left on device")
        return super().write(data)

    def truncate(self, size=None) -> int:
        if self.cut_fails:
            raise OSError(5, "Input/output error")
        return super().truncate(size)


def test_a_write_that_fails_partway_is_cut_back_off_the_file():
    """So the file still ends with a whole second, and the same second can be written again (review finding L8)."""
    stream = FailingStream()
    segment = SegmentFile(stream, BASE)
    first = segment.write_second(0, second(1_000_000_000))
    before = stream.getvalue()
    stream.fail_writes = 1
    with pytest.raises(OSError, match="No space"):
        segment.write_second(1, second(1_001_000_000))
    assert stream.getvalue() == before  # the half-written second is gone
    retried = segment.write_second(1, second(1_001_000_000))
    assert retried[0] == first[1]  # where the failed one would have gone
    seconds, good_length = read_segment(stream.getvalue(), BASE)
    assert [block.seq for block in seconds] == [0, 1] and good_length == len(stream.getvalue())


def test_a_failed_fsync_is_cut_back_too(tmp_path, monkeypatch):
    path = tmp_path / "seg.candump.zst"
    segment = SegmentFile(path, BASE)
    first = segment.write_second(0, second(1_000_000_000))
    real_fsync, calls = os.fsync, []

    def fsync_fails_once(fd: int) -> None:
        calls.append(fd)
        if len(calls) == 1:
            raise OSError(5, "Input/output error")
        real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fsync_fails_once)
        with pytest.raises(OSError, match="Input/output"):
            segment.write_second(1, second(1_001_000_000))
        assert path.stat().st_size == first[1]  # the unsynced second is cut back off, and the cut synced
    segment.write_second(1, second(1_001_000_000))
    segment.close()
    assert [block.seq for block in read_segment(path, BASE)[0]] == [0, 1]


def test_a_segment_that_cannot_be_cut_back_writes_nothing_more():
    """Whatever follows a torn second can't be read, so nothing is written after one."""
    stream = FailingStream()
    segment = SegmentFile(stream, BASE)
    segment.write_second(0, second(1_000_000_000))
    stream.fail_writes, stream.cut_fails = 1, True
    with pytest.raises(OSError, match="No space"):
        segment.write_second(1, second(1_001_000_000))
    torn = stream.getvalue()
    with pytest.raises(OSError, match="can't take another second"):
        segment.write_second(2, second(1_002_000_000))
    assert stream.getvalue() == torn
    assert [block.seq for block in read_segment(torn, BASE)[0]] == [0]


def test_a_file_that_takes_no_bytes_is_a_failed_write():
    stream = FailingStream()
    segment = SegmentFile(stream, BASE)
    stream.takes_nothing = True
    with pytest.raises(OSError, match="took no bytes"):
        segment.write_second(0, second(1_000_000_000))
    stream.takes_nothing = False
    segment.write_second(0, second(1_000_000_000))
    assert [block.seq for block in read_segment(stream.getvalue(), BASE)[0]] == [0]


def test_one_second_can_be_read_by_its_place(tmp_path):
    seconds = [second(1_000_000_000), second(1_001_000_000, 7)]
    places = write(tmp_path / "seg.candump.zst", seconds)
    block = read_second_at(tmp_path / "seg.candump.zst", *places[1], BASE)
    assert block.seq == 1 and block.frames == tuple(seconds[1])


def test_zstd_tools_see_plain_candump_text(tmp_path):
    """zstd -d skips the index frames, so the file decompresses to candump text that can-utils reads."""
    seconds = [second(1_000_000_000), second(1_001_000_000)]
    write(tmp_path / "seg.candump.zst", seconds)
    with open(tmp_path / "seg.candump.zst", "rb") as raw:
        text = zstandard.ZstdDecompressor().stream_reader(raw, read_across_frames=True).read().decode("ascii")
    assert text == "".join(candump_line(frame, BASE) for frames in seconds for frame in frames)


def test_a_segment_never_overwrites_or_appends_to_an_existing_file(tmp_path):
    (tmp_path / "seg.candump.zst").write_bytes(b"from an earlier run")
    with pytest.raises(FileExistsError):
        SegmentFile(tmp_path / "seg.candump.zst", BASE)


@pytest.mark.parametrize(
    "frames",
    [
        [],
        [Frame(1_000_000_000, 0x800, b"")],  # a standard ID above 0x7FF
        [Frame(1_000_000_000, 0x20000000, b"", extended=True)],  # past 29 bits
        [Frame(1_000_000_000, 0x20000000, b"", error=True)],  # an error type that would collide with the flag
        [Frame(1_000_000_000, 0x7E0, bytes(9))],  # more than 8 bytes
    ],
)
def test_frames_the_format_cannot_hold_are_refused(frames):
    with pytest.raises(ValueError):
        SegmentFile(io.BytesIO(), BASE).write_second(0, frames)


def test_a_file_cut_at_any_byte_reads_every_complete_second_and_no_more(tmp_path):
    """A crash can end the file anywhere: the reader keeps exactly the seconds that were written in full."""
    seconds = [second(1_000_000_000, 2), second(1_001_000_000, 1), second(1_002_000_000, 3)]
    places = write(tmp_path / "full.candump.zst", seconds)
    full = (tmp_path / "full.candump.zst").read_bytes()
    ends = [offset + length for offset, length in places]
    for cut in range(len(full) + 1):
        (tmp_path / "cut.candump.zst").write_bytes(full[:cut])
        read, good_length = read_segment(tmp_path / "cut.candump.zst", BASE)
        complete = sum(end <= cut for end in ends)
        assert len(read) == complete, cut
        assert good_length == (ends[complete - 1] if complete else 0), cut


@pytest.mark.parametrize("where", ["index", "data"])
def test_a_damaged_second_ends_the_readable_part(tmp_path, where):
    seconds = [second(1_000_000_000), second(1_001_000_000), second(1_002_000_000)]
    places = write(tmp_path / "seg.candump.zst", seconds)
    damaged = bytearray((tmp_path / "seg.candump.zst").read_bytes())
    offset, length = places[1]
    damaged[offset + 12 if where == "index" else offset + length - 2] ^= 0xFF
    (tmp_path / "seg.candump.zst").write_bytes(bytes(damaged))
    read, good_length = read_segment(tmp_path / "seg.candump.zst", BASE)
    assert [block.seq for block in read] == [0] and good_length == places[0][1]
    with pytest.raises(SegmentDamaged):
        read_second_at(tmp_path / "seg.candump.zst", offset, length, BASE)


def test_an_index_that_disagrees_with_its_data_is_damage(tmp_path):
    """The index says three frames; a data frame that decompresses to two lines is caught, not trusted."""
    import struct

    from lasto.storage import segments

    text = "".join(candump_line(frame, BASE) for frame in second(1_000_000_000, 2)).encode("ascii")
    data = zstandard.ZstdCompressor(write_checksum=True).compress(text)
    index = segments._INDEX.pack(segments._TAG, segments.FORMAT_VERSION, 0, 1_000_000_000, 1_000_001_000, 3, len(data))
    (tmp_path / "seg.candump.zst").write_bytes(struct.pack("<II", segments.SKIPPABLE_MAGIC, len(index)) + index + data)
    assert read_segment(tmp_path / "seg.candump.zst", BASE) == ([], 0)


frames_strategy = st.builds(
    Frame,
    hw_us=st.integers(1_000_000_000, 1_000_999_999),
    can_id=st.integers(0, 0x7FF),
    data=st.binary(max_size=8),
    rtr=st.just(False),
)


@given(st.lists(frames_strategy, min_size=1, max_size=40))
def test_any_second_of_frames_round_trips(frames):
    frames = sorted(frames, key=lambda frame: frame.hw_us)
    buffer = io.BytesIO()
    SegmentFile(buffer, BASE).write_second(0, frames)
    [block], _ = read_segment(buffer.getvalue(), BASE)
    assert block.frames == tuple(frames)
