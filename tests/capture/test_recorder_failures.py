"""A storage failure while recording never loses a frame the recorder was given (review finding L8).

The recorder keeps every second until it commits. After a failure it stops writing until close(), which
tries once more: what reaches the segment but not the database is left for the next capture's recovery,
with the session open, and what reaches neither is counted.
"""

from __future__ import annotations

import functools
import sqlite3

import pytest

from lasto.capture.recorder import FLUSH_AFTER, Recorder
from lasto.capture.recovery import recover
from lasto.records import BusEvent, Frame
from lasto.sim.clock import FakeClock
from lasto.storage import capture_db
from lasto.storage.root import DataRoot
from lasto.storage.segments import SegmentFile, TimeBase, read_segment

HW0 = 1_000_000_000  # the first frame's hardware timestamp
NOW = "2026-09-26T19:00:00+00:00"


class Fails:
    """Stands in for a storage call: the calls numbered in `failing` raise, the rest reach the real one."""

    def __init__(self, real, failing, error: Exception) -> None:
        self.real = real
        self.failing = failing
        self.error = error
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.failing(self.calls):
            raise self.error
        return self.real(*args, **kwargs)

    def __get__(self, instance, owner):
        """As a method, it takes the instance first, as the method it stands in for does."""
        return self if instance is None else functools.partial(self, instance)


def disk_error() -> sqlite3.OperationalError:
    return sqlite3.OperationalError("disk I/O error")


def frames_for(second: int, count: int = 4, can_id: int = 0x025) -> list[Frame]:
    start = HW0 + second * 1_000_000
    return [Frame(start + 10_000 * i, can_id, bytes([second, i, 0, 0, 0, 0, 0, 0])) for i in range(count)]


def traffic(seconds: int) -> list[Frame]:
    return [frame for second in range(seconds) for frame in frames_for(second)]


def new_run(conn) -> str:
    return capture_db.create_run(
        conn,
        started_utc="2026-09-26T18:00:00+00:00",
        pid=1,
        mode="passive",
        interface="simulator",
        channel="PCAN_USBBUS1",
        lasto_version="0.0.1",
        safety_config="{}",
        audit_path="audit/run.jsonl",
    )


def record(conn, root, frames: list[Frame], *, close: bool = True) -> Recorder:
    recorder = Recorder.start(conn, root, run_id=new_run(conn), vehicle_id="vehicle", first=frames[0], clock=FakeClock())
    for frame in frames:
        recorder.add(frame)
    if close:
        recorder.close("stopped")
    return recorder


def index_of(conn) -> tuple[list[tuple], ...]:
    """Everything the recorder writes about a session's traffic, without the session's own key."""
    return (
        conn.execute(
            "SELECT second, segment_seq, byte_offset, byte_length, frames, error_frames, first_hw_us, last_hw_us"
            " FROM seconds ORDER BY second"
        ).fetchall(),
        conn.execute(
            "SELECT can_id, extended, second, frames, first_hw_us, last_hw_us, gap_min_us, gap_max_us, dlc_min,"
            " dlc_max, changed_bits, last_data FROM id_seconds ORDER BY second, can_id"
        ).fetchall(),
        conn.execute("SELECT seq, frames, stored_bytes FROM segments ORDER BY seq").fetchall(),
        conn.execute("SELECT frames, error_frames, stored_bytes, last_hw_us FROM sessions").fetchall(),
    )


def state(conn) -> str:
    return conn.execute("SELECT state FROM sessions").fetchone()[0]


def blocks_in_segment(conn, root) -> list[int]:
    """The second numbers the segment file holds, in order, as a reader sees them."""
    [(path,)] = conn.execute("SELECT path FROM segments").fetchall()
    base = TimeBase(*conn.execute("SELECT base_hw_us, base_utc_us FROM sessions").fetchone())
    seconds, _ = read_segment(root.absolute(path), base)
    return [second.seq for second in seconds]


@pytest.fixture
def root(tmp_path) -> DataRoot:
    root = DataRoot(tmp_path / "data")
    root.ensure()
    return root


@pytest.fixture
def conn(root, opened):
    return opened(capture_db.open_capture(root))


@pytest.fixture
def clean(tmp_path, opened):
    """What the recorder writes for the same frames when nothing fails."""

    def index(frames: list[Frame]) -> tuple[list[tuple], ...]:
        root = DataRoot(tmp_path / "clean")
        root.ensure()
        conn = opened(capture_db.open_capture(root))
        record(conn, root, frames)
        return index_of(conn)

    return index


def test_a_commit_that_fails_once_is_tried_again_at_close(conn, root, clean, monkeypatch):
    commits = Fails(capture_db.record_second, lambda call: call == 1, disk_error())
    monkeypatch.setattr(capture_db, "record_second", commits)
    frames = traffic(3)
    recorder = record(conn, root, frames, close=False)  # adding never raises
    assert isinstance(recorder.error, sqlite3.OperationalError)
    recorder.close("stopped")
    assert not recorder.left_open and state(conn) == "closed"
    assert (recorder.frames_not_written, recorder.frames_not_indexed) == (0, 0)
    monkeypatch.undo()
    assert index_of(conn) == clean(frames)  # every frame, and the rollups as if nothing had failed
    assert blocks_in_segment(conn, root) == [0, 1, 2]  # the retry committed; it didn't write the second again


def test_after_a_failure_the_recorder_waits_for_close_before_it_tries_again(conn, root, monkeypatch):
    commits = Fails(capture_db.record_second, lambda call: True, disk_error())
    monkeypatch.setattr(capture_db, "record_second", commits)
    recorder = record(conn, root, traffic(6), close=False)
    assert commits.calls == 1  # not once a second against a failing disk
    recorder.close("stopped")
    assert commits.calls == 2 and recorder.left_open  # close() tried once more


def test_a_commit_that_keeps_failing_leaves_what_reached_disk_to_recovery(conn, root, clean, monkeypatch):
    commits = Fails(capture_db.record_second, lambda call: call >= 2, disk_error())
    monkeypatch.setattr(capture_db, "record_second", commits)
    frames = traffic(4)
    recorder = record(conn, root, frames)
    assert recorder.left_open and state(conn) == "open"
    assert (recorder.frames_not_written, recorder.frames_not_indexed) == (0, 12)  # seconds 1 to 3, on disk
    assert blocks_in_segment(conn, root) == [0, 1, 2, 3]
    monkeypatch.undo()
    [recovered] = recover(conn, root, now_utc=NOW).sessions
    assert recovered.seconds_indexed == 3
    assert index_of(conn) == clean(frames)


def test_a_failed_segment_write_is_tried_again_at_close(conn, root, clean, monkeypatch):
    writes = Fails(SegmentFile.write_second, lambda call: call == 2, OSError(28, "No space left on device"))
    monkeypatch.setattr(SegmentFile, "write_second", writes)
    frames = traffic(3)
    recorder = record(conn, root, frames)
    assert isinstance(recorder.error, OSError) and not recorder.left_open
    monkeypatch.undo()
    assert index_of(conn) == clean(frames)


def test_a_segment_write_that_keeps_failing_loses_only_what_never_reached_disk(conn, root, monkeypatch):
    writes = Fails(SegmentFile.write_second, lambda call: call >= 2, OSError(28, "No space left on device"))
    monkeypatch.setattr(SegmentFile, "write_second", writes)
    recorder = record(conn, root, traffic(4))
    assert recorder.left_open
    assert (recorder.frames_not_written, recorder.frames_not_indexed) == (12, 0)  # seconds 1 to 3, only in memory
    monkeypatch.undo()
    [recovered] = recover(conn, root, now_utc=NOW).sessions
    assert recovered.seconds_indexed == 0
    assert [second for (second, *_) in index_of(conn)[0]] == [0]


def test_later_seconds_still_reach_disk_after_one_that_could_not(conn, root, monkeypatch):
    """Second 1 never reaches the segment; 2 and 3 do, and commit, since the file still ends where the index does."""
    writes = Fails(SegmentFile.write_second, lambda call: call in (2, 3), OSError(28, "No space left on device"))
    monkeypatch.setattr(SegmentFile, "write_second", writes)
    recorder = record(conn, root, traffic(4))
    assert recorder.left_open and recorder.frames_not_written == 4
    monkeypatch.undo()
    assert [second for (second, *_) in index_of(conn)[0]] == [0, 2, 3]
    assert blocks_in_segment(conn, root) == [0, 2, 3]
    [recovered] = recover(conn, root, now_utc=NOW).sessions
    assert (recovered.seconds_indexed, recovered.bytes_set_aside) == (0, 0)


def test_bus_events_that_fail_to_record_are_tried_again_at_close(conn, root, monkeypatch):
    events = Fails(capture_db.add_event, lambda call: call == 1, disk_error())
    monkeypatch.setattr(capture_db, "add_event", events)
    recorder = Recorder.start(conn, root, run_id=new_run(conn), vehicle_id="v", first=frames_for(0)[0], clock=FakeClock())
    recorder.add(BusEvent("status", 0x4, HW0))
    assert recorder.error is not None
    recorder.close("stopped")
    assert conn.execute("SELECT kind FROM events").fetchall() == [("status",)]
    assert recorder.events_not_written == 0


def test_bus_events_that_keep_failing_are_counted_and_do_not_hold_the_session_open(conn, root, monkeypatch):
    monkeypatch.setattr(capture_db, "add_event", Fails(capture_db.add_event, lambda call: True, disk_error()))
    recorder = Recorder.start(conn, root, run_id=new_run(conn), vehicle_id="v", first=frames_for(0)[0], clock=FakeClock())
    for frame in frames_for(0):
        recorder.add(frame)
    recorder.add(BusEvent("status", 0x4, HW0))
    recorder.close("stopped")
    assert recorder.events_not_written == 1 and not recorder.left_open and state(conn) == "closed"


def test_bus_events_after_a_failure_wait_for_close_with_the_frames(conn, root, monkeypatch):
    monkeypatch.setattr(capture_db, "record_second", Fails(capture_db.record_second, lambda call: call == 1, disk_error()))
    events = Fails(capture_db.add_event, lambda call: False, disk_error())
    monkeypatch.setattr(capture_db, "add_event", events)
    recorder = record(conn, root, traffic(2), close=False)
    recorder.add(BusEvent("status", 0x4, HW0 + 1_500_000))
    assert recorder.error is not None and events.calls == 0  # not tried against failing storage
    recorder.close("stopped")
    assert events.calls == 1 and not recorder.left_open
    assert conn.execute("SELECT kind FROM events").fetchall() == [("status",)]


def test_a_session_that_fails_to_close_is_left_for_recovery(conn, root, monkeypatch):
    monkeypatch.setattr(capture_db, "close_session", Fails(capture_db.close_session, lambda call: True, disk_error()))
    recorder = record(conn, root, traffic(2))
    assert recorder.left_open and recorder.error is not None and state(conn) == "open"
    monkeypatch.undo()
    [recovered] = recover(conn, root, now_utc=NOW).sessions
    assert recovered.seconds_indexed == 0 and state(conn) == "recovered"


def test_a_quiet_second_that_fails_to_flush_is_kept(conn, root, monkeypatch):
    commits = Fails(capture_db.record_second, lambda call: call == 1, disk_error())
    monkeypatch.setattr(capture_db, "record_second", commits)
    clock = FakeClock()
    recorder = Recorder.start(conn, root, run_id=new_run(conn), vehicle_id="v", first=frames_for(0)[0], clock=clock)
    for frame in frames_for(0):
        recorder.add(frame)
    clock.advance(FLUSH_AFTER)
    recorder.flush_due()
    assert recorder.error is not None and conn.execute("SELECT count(*) FROM seconds").fetchone()[0] == 0
    recorder.close("stopped")
    assert conn.execute("SELECT frames FROM sessions").fetchone() == (4,) and state(conn) == "closed"
