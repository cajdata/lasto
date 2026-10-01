"""Each second's rollup per CAN ID: how many frames, how far apart, how long, and which bits changed.

The rollups are what per-ID stats, charts of a whole drive, and the broadcast explorer's change
heatmap start from (docs/architecture.md §14.8), without reading raw frames. A gap and a change are
measured from the ID's previous frame, even one in an earlier second, so nothing is lost at a second's
boundary. Error frames aren't an ID's traffic and are left out.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from lasto.records import Frame
from lasto.storage.capture_db import IdSecond

_WIDTH = 8  # classic CAN: changes are tracked over 8 data bytes


@dataclass(frozen=True, slots=True)
class LastSeen:
    """An ID's most recent frame: when, and its data."""

    hw_us: int
    data: bytes


def _padded(data: bytes) -> bytes:
    return data.ljust(_WIDTH, b"\x00")[:_WIDTH]


class _Running:
    __slots__ = ("changed", "dlc_max", "dlc_min", "first", "frames", "gap_max", "gap_min", "last", "last_data")

    def __init__(self, frame: Frame) -> None:
        self.frames = 0
        self.first = frame.hw_us
        self.last = frame.hw_us
        self.gap_min: int | None = None
        self.gap_max: int | None = None
        self.dlc_min = len(frame.data)
        self.dlc_max = len(frame.data)
        self.changed = 0
        self.last_data = frame.data


def summarize(frames: Iterable[Frame], last: dict[tuple[int, bool], LastSeen]) -> list[IdSecond]:
    """One second's frames, rolled up per ID. `last` carries each ID's previous frame, and is updated."""
    running: dict[tuple[int, bool], _Running] = {}
    for frame in frames:
        if frame.error:
            continue
        key = (frame.can_id, frame.extended)
        now = running.get(key)
        if now is None:
            now = running[key] = _Running(frame)
        previous = last.get(key)
        if previous is not None:
            gap = frame.hw_us - previous.hw_us
            now.gap_min = gap if now.gap_min is None else min(now.gap_min, gap)
            now.gap_max = gap if now.gap_max is None else max(now.gap_max, gap)
            now.changed |= int.from_bytes(_padded(previous.data), "big") ^ int.from_bytes(_padded(frame.data), "big")
        now.frames += 1
        now.last = frame.hw_us
        now.dlc_min = min(now.dlc_min, len(frame.data))
        now.dlc_max = max(now.dlc_max, len(frame.data))
        now.last_data = frame.data
        last[key] = LastSeen(frame.hw_us, frame.data)
    return [
        IdSecond(
            can_id, extended, now.frames, now.first, now.last, now.gap_min, now.gap_max, now.dlc_min, now.dlc_max,
            now.changed.to_bytes(_WIDTH, "big"), now.last_data,
        )  # fmt: skip
        for (can_id, extended), now in running.items()
    ]
