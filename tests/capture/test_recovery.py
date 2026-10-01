"""Recovery at capture start: closing what a crash or power loss left open (lasto.capture.recovery)."""

from __future__ import annotations

import io
import json

import pytest

from lasto.capture import recorder as recorder_module
from lasto.capture import recovery
from lasto.capture.recorder import Recorder
from lasto.capture.recovery import TORN_SUFFIX, Recovery, recover
from lasto.records import Frame
from lasto.sim.clock import FakeClock
from lasto.storage import capture_db
from lasto.storage.root import DataRoot
from lasto.storage.segments import SegmentFile, TimeBase, read_segment

HW0 = 1_000_000_000  # the first frame's hardware timestamp
STARTED = "2026-09-26T18:00:00+00:00"  # FakeClock's UTC when the session starts
NOW = "2026-09-26T19:00:00+00:00"


class Crash(BaseException):
    """The process stopping partway through a write: a power loss, or a kill."""


def frames_for(second: int, count: int = 4, can_id: int = 0x025) -> list[Frame]:
    start = HW0 + second * 1_000_000
    return [Frame(start + 10_000 * i, can_id, bytes([second, i, 0, 0, 0, 0, 0, 0])) for i in range(count)]


def traffic(seconds: int) -> list[Frame]:
    """Two IDs and an error frame, so the rollups have state to carry from one second to the next."""
    frames = [Frame(HW0 + 1_005_000, 0x04, b"\x01\x19", error=True)] if seconds > 1 else []
    for second in range(seconds):
        frames += frames_for(second) + frames_for(second, 2, can_id=0x0B4)
    return sorted(frames, key=lambda frame: frame.hw_us)


def new_run(conn) -> str:
    return capture_db.create_run(
        conn,
        started_utc=STARTED,
        pid=1,
        mode="passive",
        interface="simulator",
        channel="PCAN_USBBUS1",
        lasto_version="0.0.1",
        safety_config="{}",
        audit_path="audit/run.jsonl",
    )


def record(conn, root, run, frames, *, commits: int | None = None, close: bool = True) -> Recorder:
    """Record frames as a capture does.

    With `commits`, the process stops after that many seconds reach the database: the next second is on
    disk in its segment, and not in the database. Without `close`, it stops while collecting a second.
    """
    real = capture_db.record_second
    done = 0

    def record_second(*args):
        nonlocal done
        if commits is not None and done >= commits:
            raise Crash
        done += 1
        real(*args)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(capture_db, "record_second", record_second)
        recorder = Recorder.start(conn, root, run_id=run, vehicle_id="vehicle", first=frames[0], clock=FakeClock())
        try:
            for frame in frames:
                recorder.add(frame)
            if close:
                recorder.close("stopped")
        except Crash:
            pass
    if recorder._segment_file is not None:
        recorder._segment_file.close()  # the process is gone, and its open file with it
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


def next_second(conn, seq: int, frames: list[Frame]) -> bytes:
    """The bytes the recorder would write for one more second of this session."""
    base = TimeBase(*conn.execute("SELECT base_hw_us, base_utc_us FROM sessions").fetchone())
    buffer = io.BytesIO()
    SegmentFile(buffer, base).write_second(seq, frames)
    return buffer.getvalue()


def the_segment(conn, root):
    [(path, stored)] = conn.execute("SELECT path, stored_bytes FROM segments").fetchall()
    return path, root.absolute(path), stored


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
    """What the recorder writes for the same traffic when nothing goes wrong."""

    def index(frames: list[Frame]) -> tuple[list[tuple], ...]:
        root = DataRoot(tmp_path / "clean")
        root.ensure()
        conn = opened(capture_db.open_capture(root))
        record(conn, root, new_run(conn), frames)
        return index_of(conn)

    return index


def state_of(conn) -> tuple[str, str, str]:
    return conn.execute("SELECT state, end_reason, ended_utc FROM sessions").fetchone()


def test_a_second_on_disk_but_not_in_the_database_is_indexed(conn, root, clean):
    """Each second is fsynced before it commits, so a crash between the two leaves it only on disk."""
    frames = traffic(4)
    record(conn, root, new_run(conn), frames, commits=3)
    assert conn.execute("SELECT count(*) FROM seconds").fetchone()[0] == 3
    result = recover(conn, root, now_utc=NOW)
    assert index_of(conn) == clean(frames)  # exactly what the recorder would have written, rollups included
    [session] = result.sessions
    assert (session.seconds_indexed, session.bytes_set_aside, session.damaged_segments) == (1, 0, ())
    assert state_of(conn) == ("recovered", "interrupted", "2026-09-26T18:00:03.030000+00:00")  # its last frame
    assert capture_db.sessions_left_open(conn) == []


def test_a_torn_tail_is_set_aside_and_the_segment_reads_to_its_end(conn, root, clean):
    frames = traffic(3)
    record(conn, root, new_run(conn), frames, commits=2)
    _, segment, _ = the_segment(conn, root)
    whole = segment.read_bytes()
    torn = next_second(conn, 3, frames_for(3))[:40]  # the next second, cut off partway through its write
    with open(segment, "ab") as file:
        file.write(torn)
    [session] = recover(conn, root, now_utc=NOW).sessions
    assert (session.seconds_indexed, session.bytes_set_aside) == (1, len(torn))
    assert index_of(conn) == clean(frames)
    assert segment.read_bytes() == whole
    assert segment.with_name(segment.name + TORN_SUFFIX).read_bytes() == torn  # no byte that reached the disk is lost
    base = TimeBase(*conn.execute("SELECT base_hw_us, base_utc_us FROM sessions").fetchone())
    seconds, good_length = read_segment(segment, base)
    assert len(seconds) == 3 and good_length == len(whole)


def test_a_readable_second_out_of_order_is_set_aside_with_the_tail(conn, root, clean):
    frames = traffic(3)
    record(conn, root, new_run(conn), frames, commits=2)
    _, segment, _ = the_segment(conn, root)
    stray = next_second(conn, 1, frames_for(1))  # a whole second, but one the session already has
    with open(segment, "ab") as file:
        file.write(stray)
    [session] = recover(conn, root, now_utc=NOW).sessions
    assert (session.seconds_indexed, session.bytes_set_aside) == (1, len(stray))
    assert index_of(conn) == clean(frames)


def test_a_second_still_being_collected_is_lost_and_the_rest_needs_only_closing(conn, root):
    record(conn, root, new_run(conn), traffic(3), close=False)
    before = index_of(conn)
    [session] = recover(conn, root, now_utc=NOW).sessions
    assert (session.seconds_indexed, session.bytes_set_aside, session.damaged_segments) == (0, 0, ())
    assert index_of(conn) == before
    assert state_of(conn) == ("recovered", "interrupted", "2026-09-26T18:00:01.030000+00:00")
    assert not list(root.sessions_dir.rglob("*" + TORN_SUFFIX))


def test_a_crash_in_the_first_second(conn, root, clean):
    frames = traffic(1)
    record(conn, root, new_run(conn), frames, commits=0)
    [session] = recover(conn, root, now_utc=NOW).sessions
    assert session.seconds_indexed == 1
    assert index_of(conn) == clean(frames)


def test_a_session_with_no_seconds_ends_when_it_started(conn, root):
    record(conn, root, new_run(conn), traffic(1), close=False)
    recover(conn, root, now_utc=NOW)
    assert state_of(conn) == ("recovered", "interrupted", STARTED)
    assert conn.execute("SELECT frames FROM sessions").fetchone() == (0,)


def test_a_segment_the_crash_stopped_before_its_file_existed(conn, root, monkeypatch):
    def crash(*args):
        raise Crash

    monkeypatch.setattr(recorder_module, "SegmentFile", crash)
    record(conn, root, new_run(conn), traffic(2))
    monkeypatch.undo()
    _, segment, stored = the_segment(conn, root)
    assert not segment.exists() and stored == 0
    [session] = recover(conn, root, now_utc=NOW).sessions
    assert (session.seconds_indexed, session.damaged_segments) == (0, ())


@pytest.mark.parametrize("damage", ["shortened", "deleted"])
def test_a_segment_shorter_than_its_index_is_reported_and_left_alone(conn, root, damage):
    record(conn, root, new_run(conn), traffic(3), close=False)
    path, segment, stored = the_segment(conn, root)
    if damage == "shortened":
        segment.write_bytes(segment.read_bytes()[: stored - 1])
    else:
        segment.unlink()
    [session] = recover(conn, root, now_utc=NOW).sessions
    assert session.damaged_segments == (path,)
    assert not segment.with_name(segment.name + TORN_SUFFIX).exists()
    assert not segment.exists() if damage == "deleted" else segment.stat().st_size == stored - 1
    [(host_utc, detail)] = conn.execute("SELECT host_utc, detail FROM events WHERE kind = 'recovered'").fetchall()
    assert host_utc == NOW
    assert json.loads(detail) == {"bytes_set_aside": 0, "damaged_segments": [path], "seconds_indexed": 0}
    assert state_of(conn)[0] == "recovered"


def test_a_recovery_cut_short_finishes_the_next_time(conn, root, clean, monkeypatch):
    frames = traffic(3)
    record(conn, root, new_run(conn), frames, commits=2)
    _, segment, _ = the_segment(conn, root)
    torn = next_second(conn, 3, frames_for(3))[:40]
    with open(segment, "ab") as file:
        file.write(torn)

    def crash(*args):
        raise Crash

    with monkeypatch.context() as patch:
        patch.setattr(recovery, "_set_aside", crash)
        with pytest.raises(Crash):
            recover(conn, root, now_utc=NOW)
    assert len(capture_db.sessions_left_open(conn)) == 1  # second 2 indexed, the tail still there
    [session] = recover(conn, root, now_utc=NOW).sessions
    assert (session.seconds_indexed, session.bytes_set_aside) == (0, len(torn))
    assert index_of(conn) == clean(frames)
    assert segment.with_name(segment.name + TORN_SUFFIX).read_bytes() == torn


def audit_line(event: str, utc: str) -> bytes:
    return (json.dumps({"event": event, "mono": 1.0, "utc": utc}, sort_keys=True) + "\n").encode("utf-8")


@pytest.mark.parametrize(
    ("audit", "sessions", "ended"),
    [
        # The run's last audit record is the last thing it did.
        (audit_line("a", STARTED) + audit_line("b", "2026-09-26T18:30:00+00:00") + b'{"event": "c", "ut', 1,
         "2026-09-26T18:30:00+00:00"),
        # A passive session outlasts the run's audit records.
        (audit_line("a", STARTED), 1, "2026-09-26T18:00:01.030000+00:00"),
        # Nothing but its start.
        (b"", 0, STARTED),
    ],
)  # fmt: skip
def test_a_run_left_unended_is_indexed_and_ended_at_the_last_thing_it_did(conn, root, audit, sessions, ended):
    run = new_run(conn)
    (root.audit_dir / "run.jsonl").write_bytes(audit)
    for _ in range(sessions):
        record(conn, root, run, traffic(2))
    result = recover(conn, root, now_utc=NOW)
    assert result == Recovery(sessions=(), runs=(run,))
    assert conn.execute("SELECT ended_utc, end_reason FROM runs").fetchone() == (ended, "interrupted")
    assert conn.execute("SELECT count(*) FROM audit").fetchone()[0] == audit.count(b"\n")


def test_what_ended_cleanly_is_left_alone(conn, root):
    run = new_run(conn)
    record(conn, root, run, traffic(2))
    capture_db.end_run(conn, run, ended_utc=NOW, end_reason="stopped")
    before = index_of(conn)
    assert recover(conn, root, now_utc=NOW) == Recovery(sessions=(), runs=())
    assert index_of(conn) == before
