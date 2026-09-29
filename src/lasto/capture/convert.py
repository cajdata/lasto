"""The safety core's frame types, converted to plain records (lasto.records)."""

from __future__ import annotations

from collections.abc import Iterable

from lasto.records import BusEvent, Frame, Record
from lasto.safety.frames import CanFrame, ErrorFrame, ReadError, Received, StatusMessage


def to_records(items: Iterable[Received]) -> list[Record]:
    """Everything a channel read, in order, as plain records."""
    return [to_record(item) for item in items]


def to_record(item: Received) -> Record:
    """One item a channel read, as a plain record."""
    if isinstance(item, CanFrame):
        return Frame(item.timestamp_us, item.can_id, item.data, extended=item.extended, rtr=item.rtr)
    if isinstance(item, ErrorFrame):
        return Frame(item.timestamp_us, item.error_type, item.data, error=True)
    if isinstance(item, StatusMessage):
        return BusEvent("status", item.status, item.timestamp_us)
    if isinstance(item, ReadError):
        return BusEvent("read_error", item.status)
    raise TypeError(f"not something a channel reads: {type(item).__name__}")
