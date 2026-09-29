"""The recorder: one session's frames to segment files, and each second to the capture database."""

from __future__ import annotations

import pytest

from lasto.capture.recorder import FLUSH_AFTER, Recorder
from lasto.records import BusEvent, Frame
from lasto.sim.clock import FakeClock
from lasto.storage import capture_db
from lasto.storage.root import DataRoot
from lasto.storage.segments import TimeBase, read_segment

HW0 = 1_000_000_000  # the first frame's hardware timestamp


@pytest.fixture
def root(tmp_path) -> DataRoot:
    root = DataRoot(tmp_path)
    root.ensure()
    return root


@pytest.fixture
def conn(root, opened):
    return opened(capture_db.open_capture(root))


@pytest.fixture
def run(conn) -> str:
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


def frames_for(second: int, count: int = 4, can_id: int = 0x025) -> list[Frame]:
    start = HW0 + second * 1_000_000
    return [Frame(start + 10_000 * i, can_id, bytes([second, i, 0, 0, 0, 0, 0, 0])) for i in range(count)]


def start(conn, root, run, clock, first: Frame, **options) -> Recorder:
    return Recorder.start(conn, root, run_id=run, vehicle_id="vehicle", first=first, clock=clock, **options)


def test_a_session_is_recorded_second_by_second(conn, root, run):
    clock = FakeClock()
    seconds = [frames_for(0), frames_for(1, 3), frames_for(2, 5)]
    recorder = start(conn, root, run, clock, seconds[0][0])
    for frames in seconds:
        for frame in frames:
            recorder.add(frame)
    assert recorder.frames == 12  # counted as they arrive, the last second not yet written
    recorder.close("stopped")
    row = conn.execute(
        "SELECT state, end_reason, frames, error_frames, base_hw_us, folder FROM sessions WHERE id = ?",
        (recorder.session_id,),
    ).fetchone()
    assert row[:5] == ("closed", "stopped", 12, 0, HW0)
    index = conn.execute("SELECT second, segment_seq, frames FROM seconds ORDER BY second").fetchall()
    assert index == [(0, 1, 4), (1, 1, 3), (2, 1, 5)]
    [(path,)] = conn.execute("SELECT path FROM segments").fetchall()
    assert path.startswith(row[5] + "/") and path.endswith("seg-0001.candump.zst")
    base_utc = conn.execute("SELECT base_utc_us FROM sessions").fetchone()[0]
    read, _ = read_segment(root.absolute(path), TimeBase(HW0, base_utc))
    assert [list(block.frames) for block in read] == seconds


def test_each_second_rolls_up_each_id(conn, root, run):
    clock = FakeClock()
    recorder = start(conn, root, run, clock, frames_for(0)[0])
    for frame in frames_for(0) + frames_for(0, 2, can_id=0x0B4) + frames_for(1):
        recorder.add(frame)
    recorder.close("stopped")
    rows = conn.execute("SELECT can_id, second, frames, gap_min_us FROM id_seconds ORDER BY can_id, second").fetchall()
    assert rows == [(0x025, 0, 4, 10_000), (0x025, 1, 4, 10_000), (0x0B4, 0, 2, 10_000)]
    changed = conn.execute("SELECT changed_bits FROM id_seconds WHERE can_id = 0x025 AND second = 1").fetchone()[0]
    assert changed == bytes([0x01, 0x03, 0, 0, 0, 0, 0, 0])  # byte 0: second 0 to 1; byte 1: i counting up


def test_error_frames_are_kept_and_counted_apart(conn, root, run):
    clock = FakeClock()
    first = frames_for(0)[0]
    recorder = start(conn, root, run, clock, first)
    recorder.add(first)
    recorder.add(Frame(HW0 + 5_000, 0x04, b"\x01\x19", error=True))
    recorder.close("stopped")
    assert conn.execute("SELECT frames, error_frames FROM sessions").fetchone() == (2, 1)
    assert conn.execute("SELECT count(*) FROM id_seconds").fetchone()[0] == 1


def test_a_second_is_written_once_it_is_over_even_if_the_bus_goes_quiet(conn, root, run):
    clock = FakeClock()
    recorder = start(conn, root, run, clock, frames_for(0)[0])
    for frame in frames_for(0):
        recorder.add(frame)
    recorder.flush_due()
    assert conn.execute("SELECT count(*) FROM seconds").fetchone()[0] == 0  # the second might still be going on
    clock.advance(FLUSH_AFTER)
    recorder.flush_due()
    assert conn.execute("SELECT count(*) FROM seconds").fetchone()[0] == 1
    recorder.close("bus_silent")


def test_a_new_segment_starts_on_the_hour(conn, root, run):
    clock = FakeClock()
    recorder = start(conn, root, run, clock, frames_for(0)[0], segment_seconds=2)
    for second in range(5):
        for frame in frames_for(second, 2):
            recorder.add(frame)
    recorder.close("stopped")
    segments = conn.execute("SELECT seq, frames FROM segments ORDER BY seq").fetchall()
    assert segments == [(1, 4), (2, 4), (3, 2)]
    assert [row[0] for row in conn.execute("SELECT segment_seq FROM seconds ORDER BY second")] == [1, 1, 2, 2, 3]


def test_the_database_knows_a_segment_before_its_file_exists(conn, root, run):
    """So recovery finds every segment file a capture made, even one a crash left with nothing in it."""
    clock = FakeClock()
    recorder = start(conn, root, run, clock, frames_for(0)[0])
    [(folder,)] = conn.execute("SELECT folder FROM sessions").fetchall()
    root.absolute(f"{folder}/seg-0001.candump.zst").write_bytes(b"")  # creating the file fails
    with pytest.raises(FileExistsError):
        for frame in frames_for(0) + frames_for(1):
            recorder.add(frame)
    assert conn.execute("SELECT seq, path FROM segments").fetchall() == [(1, f"{folder}/seg-0001.candump.zst")]


def test_the_time_base_and_anchors(conn, root, run):
    clock = FakeClock()
    recorder = start(conn, root, run, clock, frames_for(0)[0], anchor_every=10.0)
    for second in range(3):
        for frame in frames_for(second, 1):
            recorder.add(frame)
        clock.advance(6.0)
        recorder.flush_due()
    recorder.close("stopped")
    anchors = conn.execute("SELECT hw_us, host_monotonic FROM anchors ORDER BY hw_us").fetchall()
    assert anchors[0] == (HW0, 1000.0)  # the session's time base, when the first frame arrived
    assert len(anchors) == 2  # and one more once 10 s had passed


def test_the_session_started_and_ended_at_its_frames_times(conn, root, run):
    clock = FakeClock()
    recorder = start(conn, root, run, clock, frames_for(0)[0])
    for frame in frames_for(0) + frames_for(3):
        recorder.add(frame)
    recorder.close("stopped")
    started, ended = conn.execute("SELECT started_utc, ended_utc FROM sessions").fetchone()
    assert started == "2026-09-26T18:00:00+00:00"
    assert ended == "2026-09-26T18:00:03.030000+00:00"


def test_bus_events_are_recorded_with_the_session(conn, root, run):
    clock = FakeClock()
    recorder = start(conn, root, run, clock, frames_for(0)[0])
    recorder.add(BusEvent("read_error", 0x40))
    recorder.close("stopped")
    [(kind, detail, session)] = conn.execute("SELECT kind, detail, session FROM events").fetchall()
    assert (kind, detail) == ("read_error", '{"status": "0x00040"}') and session is not None


def test_a_closed_recorder_takes_nothing_more(conn, root, run):
    clock = FakeClock()
    recorder = start(conn, root, run, clock, frames_for(0)[0])
    recorder.close("stopped")
    with pytest.raises(RuntimeError):
        recorder.add(frames_for(1)[0])
    recorder.close("again")  # closing twice does nothing
    assert conn.execute("SELECT end_reason FROM sessions").fetchone() == ("stopped",)
