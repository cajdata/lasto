"""Raw capture segments: candump text, one checksummed zstd frame per second of traffic (§4).

Each second of traffic is two zstd frames, back to back:
1. A skippable frame holding the second's index: a format tag, the sequence number, the first and
   last hardware timestamps, the frame count, and the length of the data frame after it.
2. A zstd data frame with a content checksum, holding that second's frames as candump lines:
   `(1759168800.000123) can0 7E0#0201000000000000`.

`zstd -d` skips the skippable frames, so a segment decompresses to plain candump text that can-utils
reads.

Durability:
- The writer fsyncs after every second, and it never opens a file that already exists.
- The reader stops at the first truncated or corrupt frame, so a crash costs at most the second
  being written.

Timestamps are UTC in integer microseconds, derived from the hardware clock through the session's
time base, so they convert back to hardware time exactly.
"""

from __future__ import annotations

import os
import re
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import zstandard

from lasto.records import Frame

SKIPPABLE_MAGIC = 0x184D2A5C  # one of zstd's sixteen skippable-frame magic numbers
FORMAT_VERSION = 1
_TAG = b"LSEC"
# tag, format version, sequence, first and last hardware timestamps (us), frame count, data frame length
_INDEX = struct.Struct("<4sBIqqII")
_HEADER = struct.Struct("<II")  # skippable magic, payload length
INTERFACE = "can0"
CAN_ERR_FLAG = 0x20000000
MAX_EXTENDED_ID = 0x1FFFFFFF
MAX_STANDARD_ID = 0x7FF
_LINE = re.compile(r"\((\d+)\.(\d{6})\) (\S+) ([0-9A-F]{3}|[0-9A-F]{8})#(R|(?:[0-9A-F]{2}){0,8})")


class SegmentDamaged(Exception):
    """A second of a segment is truncated, corrupt, or not in the format; nothing from it is trusted."""


@dataclass(frozen=True, slots=True)
class TimeBase:
    """Where a session's hardware clock and UTC meet: one hardware timestamp and its UTC, both in microseconds."""

    hw_us: int
    utc_us: int

    def utc_us_of(self, hw_us: int) -> int:
        return self.utc_us + (hw_us - self.hw_us)

    def hw_us_of(self, utc_us: int) -> int:
        return self.hw_us + (utc_us - self.utc_us)


@dataclass(frozen=True, slots=True)
class Second:
    """One second of a segment, as read back: its index, its frames, and where it is in the file."""

    seq: int
    first_hw_us: int
    last_hw_us: int
    frames: tuple[Frame, ...]
    offset: int
    length: int


def candump_line(frame: Frame, base: TimeBase) -> str:
    """One frame as a candump log line, with its newline."""
    if len(frame.data) > 8:
        raise ValueError("a classic CAN frame holds at most 8 bytes")
    if frame.error:
        if not 0 <= frame.can_id <= MAX_EXTENDED_ID:
            raise ValueError(f"error type {frame.can_id:#x} doesn't fit beside the error flag")
        can_id = f"{CAN_ERR_FLAG | frame.can_id:08X}"
    elif frame.extended:
        if not 0 <= frame.can_id <= MAX_EXTENDED_ID:
            raise ValueError(f"extended ID {frame.can_id:#x} is more than 29 bits")
        can_id = f"{frame.can_id:08X}"
    else:
        if not 0 <= frame.can_id <= MAX_STANDARD_ID:
            raise ValueError(f"standard ID {frame.can_id:#x} is more than 11 bits")
        can_id = f"{frame.can_id:03X}"
    utc_us = base.utc_us_of(frame.hw_us)
    data = "R" if frame.rtr else frame.data.hex().upper()
    return f"({utc_us // 1_000_000}.{utc_us % 1_000_000:06d}) {INTERFACE} {can_id}#{data}\n"


def parse_candump_line(line: str, base: TimeBase) -> Frame:
    """A candump log line written by candump_line, back as a frame."""
    match = _LINE.fullmatch(line.rstrip("\n"))
    if match is None:
        raise SegmentDamaged(f"not a candump line: {line!r}")
    seconds, micros, _interface, can_id_text, data_text = match.groups()
    hw_us = base.hw_us_of(int(seconds) * 1_000_000 + int(micros))
    can_id = int(can_id_text, 16)
    rtr = data_text == "R"
    data = b"" if rtr else bytes.fromhex(data_text)
    if len(can_id_text) == 3:
        if can_id > MAX_STANDARD_ID:
            raise SegmentDamaged(f"standard ID {can_id_text} is more than 11 bits")
        return Frame(hw_us, can_id, data, rtr=rtr)
    if can_id & ~(CAN_ERR_FLAG | MAX_EXTENDED_ID):
        raise SegmentDamaged(f"ID {can_id_text} sets flag bits candump logs don't hold")
    if can_id & CAN_ERR_FLAG:
        return Frame(hw_us, can_id & MAX_EXTENDED_ID, data, error=True)
    return Frame(hw_us, can_id, data, extended=True, rtr=rtr)


class SegmentFile:
    """One segment file being written, a second at a time, each fsynced before write_second returns."""

    def __init__(self, target: Path | BinaryIO, base: TimeBase) -> None:
        """A path opens a new file (never one that exists); a binary stream is written as it is (tests)."""
        self._owned = isinstance(target, str | os.PathLike)
        self._file: BinaryIO = open(target, "xb") if self._owned else target  # type: ignore[assignment,arg-type]
        self._base = base
        self._compressor = zstandard.ZstdCompressor(level=3, write_checksum=True, write_content_size=True)
        self._offset = 0

    def write_second(self, seq: int, frames: Sequence[Frame]) -> tuple[int, int]:
        """Write one second's frames, in order. Returns where it went: (byte offset, byte length)."""
        if not frames:
            raise ValueError("a second in a segment holds at least one frame")
        text = "".join(candump_line(frame, self._base) for frame in frames).encode("ascii")
        data = self._compressor.compress(text)
        index = _INDEX.pack(_TAG, FORMAT_VERSION, seq, frames[0].hw_us, frames[-1].hw_us, len(frames), len(data))
        block = _HEADER.pack(SKIPPABLE_MAGIC, len(index)) + index + data
        self._file.write(block)
        self._file.flush()
        if self._owned:
            os.fsync(self._file.fileno())
        offset, self._offset = self._offset, self._offset + len(block)
        return offset, len(block)

    def close(self) -> None:
        if self._owned:
            self._file.close()


def _read_block(data: bytes, pos: int, base: TimeBase) -> Second:
    header = data[pos : pos + _HEADER.size]
    if len(header) < _HEADER.size:
        raise SegmentDamaged("the file ends inside a frame header")
    magic, size = _HEADER.unpack(header)
    if magic != SKIPPABLE_MAGIC or size != _INDEX.size:
        raise SegmentDamaged("no second index where one should start")
    start = pos + _HEADER.size
    index = data[start : start + size]
    if len(index) < size:
        raise SegmentDamaged("the file ends inside a second index")
    tag, version, seq, first_hw_us, last_hw_us, count, data_length = _INDEX.unpack(index)
    if tag != _TAG or version != FORMAT_VERSION:
        raise SegmentDamaged("the second index isn't in this format")
    blob = data[start + size : start + size + data_length]
    if len(blob) < data_length:
        raise SegmentDamaged("the file ends inside a second's data")
    try:
        text = zstandard.ZstdDecompressor().decompress(blob).decode("ascii")
    except (zstandard.ZstdError, UnicodeDecodeError) as exc:
        raise SegmentDamaged(f"a second's data is corrupt: {exc}") from None
    frames = tuple(parse_candump_line(line, base) for line in text.splitlines())
    if len(frames) != count or frames[0].hw_us != first_hw_us or frames[-1].hw_us != last_hw_us:
        raise SegmentDamaged("a second's data doesn't match its index")
    return Second(seq, first_hw_us, last_hw_us, frames, pos, _HEADER.size + size + data_length)


def read_segment(source: Path | bytes, base: TimeBase) -> tuple[list[Second], int]:
    """Every complete second in a segment, and the length of the part that holds them.

    Reading stops at the first truncated or damaged frame: whatever follows it isn't trusted.
    """
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    seconds: list[Second] = []
    pos = 0
    while pos < len(data):
        try:
            second = _read_block(data, pos, base)
        except SegmentDamaged:
            break
        seconds.append(second)
        pos += second.length
    return seconds, pos


def read_second_at(path: Path, offset: int, length: int, base: TimeBase) -> Second:
    """One second, read by the place the capture database recorded for it."""
    with open(path, "rb") as file:
        file.seek(offset)
        data = file.read(length)
    second = _read_block(data, 0, base)
    if second.length != length:
        raise SegmentDamaged("the second at that place isn't the recorded length")
    return Second(second.seq, second.first_hw_us, second.last_hw_us, second.frames, offset, length)
