"""A passive session whose audit log fails, as a full disk would make it: the channel still closes, nothing is
reopened on top of a failing log, the log is let go, and the failure still reaches the caller.

The website session's claim check (2026-10-01) found the first of these: a failed session_closed write raised out
of PassiveSession.close() before the log was detached.
"""

from __future__ import annotations

import pytest
from helpers import CHANNEL, HANDLE

from lasto.safety import pcan_constants as pc
from lasto.safety.audit import REFUSALS, Auditor
from lasto.safety.reader import STATUS_INTERVAL
from lasto.safety.session import open_passive_session


class FailsOn:
    """An audit sink that can't write the named events, and keeps every other record."""

    def __init__(self, *events: str) -> None:
        self.events = set(events)
        self.records: list[dict] = []

    def write(self, record: dict) -> None:
        if record["event"] in self.events:
            raise OSError(28, "No space left on device")
        self.records.append(record)


def passive(sim, sink: FailsOn):
    return open_passive_session(CHANNEL, auditor=Auditor(sink, sim.clock), clock=sim.clock, library=sim.dll)


def still_attached(sink: FailsOn) -> bool:
    """Whether the refusal log still writes to this sink: a refusal recorded now reaches it only if it is."""
    REFUSALS.record(transport="pcan", reason="probe", detail="is the log still attached?", request="attachment probe")
    return any(record.get("request") == "attachment probe" for record in sink.records)


def test_a_close_that_cannot_be_recorded_still_closes_and_lets_the_log_go(sim):
    sink = FailsOn("session_closed")
    session = passive(sim, sink)
    with pytest.raises(OSError, match="No space"):
        session.close()
    assert not sim.dll.channel(HANDLE).initialized  # the channel closed first
    assert session.ended
    assert not still_attached(sink)
    session.close()  # closing again does nothing


def test_a_distrusted_channel_closes_even_if_why_cannot_be_recorded(sim):
    """A channel that lost listen-only is never left running, and nothing is reopened on top of a failing log."""
    sink = FailsOn("passive_channel_distrusted")
    session = passive(sim, sink)
    sim.dll.channel(HANDLE).listen_only = pc.PCAN_PARAMETER_OFF
    sim.clock.advance(STATUS_INTERVAL)
    with pytest.raises(OSError, match="No space"):
        session.pump()
    assert not sim.dll.channel(HANDLE).initialized
    assert sim.dll.calls.count("CAN_Initialize") == 1  # not reopened
    assert session.ended and "audit log" in session.end_reason
    assert not still_attached(sink)
    assert session.pump() == []


def test_a_reopen_that_cannot_be_recorded_is_closed_again(sim):
    sink = FailsOn("passive_channel_reopened")
    session = passive(sim, sink)
    sim.dll.channel(HANDLE).listen_only = pc.PCAN_PARAMETER_OFF
    sim.clock.advance(STATUS_INTERVAL)
    with pytest.raises(OSError, match="No space"):
        session.pump()
    assert not sim.dll.channel(HANDLE).initialized  # the reopened channel too
    assert session.ended and not still_attached(sink)


def test_a_failed_reopen_that_cannot_be_recorded_ends_the_session(sim):
    sink = FailsOn("passive_reopen_failed")
    session = passive(sim, sink)
    sim.dll.fail_get[pc.PCAN_LISTEN_ONLY] = pc.PCAN_ERROR_ILLPARAMTYPE  # every reopen refuses to trust the channel
    sim.clock.advance(STATUS_INTERVAL)
    with pytest.raises(OSError, match="No space"):
        session.pump()
    assert not sim.dll.channel(HANDLE).initialized
    assert session.ended and not still_attached(sink)


def test_a_session_that_ends_itself_lets_the_log_go_even_if_the_end_cannot_be_recorded(sim):
    sink = FailsOn("session_ended")
    session = passive(sim, sink)
    sim.dll.fail_get[pc.PCAN_LISTEN_ONLY] = pc.PCAN_ERROR_ILLPARAMTYPE
    sim.clock.advance(STATUS_INTERVAL)
    with pytest.raises(OSError, match="No space"):
        session.pump()
    assert not sim.dll.channel(HANDLE).initialized
    assert session.ended and not still_attached(sink)
