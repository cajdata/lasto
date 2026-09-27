"""Rule 7 triggers in the reader: error frames, bus state, overruns, interface failure, subscriber errors."""

from collections import deque

import pytest

from lasto.safety import pcan_constants as pc
from lasto.safety.frames import CanFrame, ErrorFrame, ReadError, StatusMessage
from lasto.safety.killswitch import KillSwitch
from lasto.safety.reader import STATUS_INTERVAL, Reader

FRAME = CanFrame(0x025, b"\x07\xff", 1)


class StubChannel:
    def __init__(self, *batches, statuses=(), listen_only=(True,)):
        self.batches = deque(batches)
        self.statuses = deque(statuses)
        self.listen_only_answers = deque(listen_only)
        self.status_calls = 0
        self.listen_only_calls = 0

    def drain(self):
        return self.batches.popleft() if self.batches else []

    def status(self):
        self.status_calls += 1
        return self.statuses.popleft() if self.statuses else pc.PCAN_ERROR_OK

    def listen_only(self):
        self.listen_only_calls += 1
        return self.listen_only_answers.popleft() if len(self.listen_only_answers) > 1 else self.listen_only_answers[0]


def polled_reader(clock, *batches, statuses=()):
    killswitch = KillSwitch()
    seen = []
    reader = Reader(StubChannel(*batches, statuses=statuses), clock, killswitch=killswitch, subscribers=[seen.append])
    return reader, killswitch, seen


def test_frames_reach_subscribers_in_order(clock):
    reader, killswitch, seen = polled_reader(clock, [FRAME, FRAME])
    later = []
    reader.subscribe(later.append)
    assert reader.poll_once() == [FRAME, FRAME]
    assert seen == later == [FRAME, FRAME]
    assert not killswitch.tripped


@pytest.mark.parametrize(
    ("item", "cause"),
    [
        (ErrorFrame(1, b"\x01", 0), "error_frame"),
        (StatusMessage(pc.PCAN_ERROR_BUSPASSIVE, 0), "bus_error_state"),
        (StatusMessage(pc.PCAN_ERROR_BUSOFF, 0), "bus_error_state"),
        (ReadError(pc.PCAN_ERROR_QOVERRUN), "receive_overrun"),
        (ReadError(pc.PCAN_ERROR_OVERRUN), "receive_overrun"),
        (ReadError(pc.PCAN_ERROR_ILLHW), "interface_failed"),
        (StatusMessage(pc.PCAN_ERROR_ILLOPERATION, 0), "interface_failed"),
    ],
)
def test_polled_kill_triggers(clock, item, cause):
    reader, killswitch, seen = polled_reader(clock, [item])
    reader.poll_once()
    assert killswitch.cause == cause
    assert seen == [item]  # the recorder still gets it


def test_warning_levels_are_only_recorded(clock):
    reader, killswitch, _ = polled_reader(
        clock, [StatusMessage(pc.PCAN_ERROR_BUSHEAVY, 0), StatusMessage(pc.PCAN_ERROR_BUSLIGHT, 0), StatusMessage(0, 0)]
    )
    reader.poll_once()
    assert not killswitch.tripped


def test_status_is_polled_on_an_interval(clock):
    reader, killswitch, _ = polled_reader(clock, statuses=[pc.PCAN_ERROR_OK, pc.PCAN_ERROR_BUSOFF])
    reader.poll_once()
    reader.poll_once()  # too soon to ask again
    assert reader._channel.status_calls == 1
    clock.advance(STATUS_INTERVAL)
    reader.poll_once()
    assert killswitch.cause == "bus_error_state"


def test_unplugged_adapter_stops_the_reader(clock):
    reader, killswitch, _ = polled_reader(clock, [ReadError(pc.PCAN_ERROR_ILLHW)], [FRAME])
    reader.poll_once()
    assert reader.failed and reader.failed_status == pc.PCAN_ERROR_ILLHW
    assert reader._channel.status_calls == 0  # not asked after it failed
    assert reader.poll_once() == []
    assert killswitch.cause == "interface_failed"


def test_unplug_found_by_the_status_poll(clock):
    reader, killswitch, _ = polled_reader(clock, statuses=[pc.PCAN_ERROR_ILLHW])
    reader.poll_once()
    assert reader.failed and killswitch.tripped


def test_polled_subscriber_error_trips_but_capture_continues(clock):
    reader, killswitch, seen = polled_reader(clock, [FRAME, FRAME])

    def broken(item):
        raise KeyError("bug")

    reader._subscribers.insert(0, broken)
    reader.poll_once()
    assert killswitch.cause == "subscriber_error:KeyError"
    assert seen == [FRAME, FRAME]


def test_passive_reader_records_bus_state_without_a_kill_switch(clock):
    seen = []
    items = [ErrorFrame(1, b"", 0), StatusMessage(pc.PCAN_ERROR_BUSPASSIVE, 0), ReadError(pc.PCAN_ERROR_QOVERRUN)]
    reader = Reader(StubChannel(items), clock, subscribers=[seen.append])
    reader.poll_once()
    assert seen == items
    assert not reader.failed


def test_polled_readers_do_not_check_listen_only(clock):
    reader, _, _ = polled_reader(clock, [StatusMessage(pc.PCAN_ERROR_OK, 0)])
    reader.poll_once()
    assert reader._channel.listen_only_calls == 0


def test_passive_reader_rechecks_listen_only_every_status_interval(clock, auditor, sink):
    channel = StubChannel(listen_only=(True, True, False))
    reader = Reader(channel, clock, verify_listen_only=True, auditor=auditor)
    reader.poll_once()
    reader.poll_once()  # too soon
    assert channel.listen_only_calls == 1
    clock.advance(STATUS_INTERVAL)
    reader.poll_once()
    assert not reader.failed
    clock.advance(STATUS_INTERVAL)
    reader.poll_once()
    assert (reader.failed, reader.failure_reason, reader.failed_status) == (True, "listen_only_lost", None)
    assert STATUS_INTERVAL <= 0.1


def test_controller_activated_triggers_an_immediate_recheck(clock, auditor, sink):
    activated = StatusMessage(pc.PCAN_ERROR_OK, 0)
    channel = StubChannel([activated], [], [activated], listen_only=(True, True, False))
    reader = Reader(channel, clock, verify_listen_only=True, auditor=auditor)
    reader.poll_once()  # recheck from the message, then the first status tick
    assert channel.listen_only_calls == 2 and not reader.failed
    assert [r["trigger"] for r in sink.records if r["event"] == "listen_only_rechecked"] == ["controller_activated"]
    reader.poll_once()
    reader.poll_once()  # same interval: only the message triggers a check, and it fails
    assert reader.failure_reason == "listen_only_lost"
    assert channel.listen_only_calls == 3


def test_controller_activated_without_an_auditor(clock):
    channel = StubChannel([StatusMessage(pc.PCAN_ERROR_OK, 0)])
    reader = Reader(channel, clock, verify_listen_only=True)
    reader.poll_once()
    assert not reader.failed


def test_passive_reader_stops_on_interface_failure(clock):
    reader = Reader(StubChannel([ReadError(pc.PCAN_ERROR_ILLHW)]), clock, verify_listen_only=True)
    reader.poll_once()
    assert (reader.failure_reason, reader.failed_status) == ("interface_failed", pc.PCAN_ERROR_ILLHW)
    assert reader._channel.listen_only_calls == 0


def test_passive_subscriber_errors_propagate(clock):
    def broken(item):
        raise ValueError("recorder bug")

    reader = Reader(StubChannel([FRAME]), clock, subscribers=[broken])
    with pytest.raises(ValueError):
        reader.poll_once()
