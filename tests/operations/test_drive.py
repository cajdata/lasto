"""lasto drive: passive capture in armed mode, from the simulator (lasto.operations.drive).

Every test here drives the simulator: the stand-in DLL, on the simulator's own clock. The test plugin
fails any test in which the simulator receives a frame, so each one also proves passive capture
transmits nothing.
"""

from __future__ import annotations

import json
import signal
import sqlite3
import threading
import time

import pytest

from lasto.capture.recovery import recover
from lasto.operations.drive import DriveResult, drive, simulated, truck
from lasto.operations.keep_awake import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, KeepAwake
from lasto.safety import pcan_constants as pc
from lasto.safety.clock import SystemClock
from lasto.safety.errors import InterfaceError
from lasto.services import safety_config
from lasto.sim.vehicle import Sim
from lasto.storage import capture_db
from lasto.storage.capture_lock import CaptureLock, CaptureRunning, capture_running
from lasto.storage.live_db import read_live
from lasto.storage.root import DataRoot
from lasto.storage.segments import TimeBase, read_segment

HANDLE = pc.USB_CHANNELS[1]  # PCAN_USBBUS1


@pytest.fixture
def root(tmp_path) -> DataRoot:
    return DataRoot(tmp_path / "data")


@pytest.fixture
def on_bus(sim: Sim) -> list[float]:
    """The time of every frame the simulated bus carries, counted by the simulator itself."""
    times: list[float] = []
    sim.bus.add_tap("count", lambda time, can_id, data: times.append(time))
    return times


@pytest.fixture
def lines() -> list[str]:
    return []


@pytest.fixture
def reader(root, opened):
    def read():
        return opened(capture_db.read_capture(root))

    return read


def run(root, sim, lines, seconds: float = 10.0, **options) -> DriveResult:
    return drive(root, simulated(sim), seconds=seconds, report=lines.append, **options)


def sessions(conn) -> list[tuple]:
    return conn.execute(
        "SELECT id, state, end_reason, frames, error_frames, started_utc, ended_utc FROM sessions ORDER BY key"
    ).fetchall()


def test_a_simulated_drive_records_every_frame_the_bus_carried(root, sim, on_bus, lines, reader):
    result = run(root, sim, lines)
    conn = reader()
    [(session, state, reason, frames, errors, started, _)] = sessions(conn)
    assert (state, reason, errors) == ("closed", "time_limit", 0)
    assert frames == len(on_bus) == result.frames > 2900  # 300 a second, every one of them
    assert result.sessions == (session,) and result.end_reason == "time_limit"
    assert started == "2026-09-26T18:00:00+00:00"
    # The segment holds exactly those frames, readable on their own.
    [(path,)] = conn.execute("SELECT path FROM segments").fetchall()
    base = TimeBase(*conn.execute("SELECT base_hw_us, base_utc_us FROM sessions").fetchone())
    seconds, _ = read_segment(root.absolute(path), base)
    assert sum(len(second.frames) for second in seconds) == frames
    assert sim.dll.writes == []  # passive: nothing written to the bus


def test_the_run_records_what_it_ran_on_and_under(root, sim, lines, reader):
    result = run(root, sim, lines, seconds=2.0)
    conn = reader()
    row = conn.execute(
        "SELECT id, mode, interface, channel, hardware, api_version, safety_config, audit_path, end_reason FROM runs"
    ).fetchone()
    assert row[:6] == (result.run_id, "passive", "simulator", "PCAN_USBBUS1", "PCAN-USB (simulated)", "4.7.0.11")
    assert json.loads(row[6]) == json.loads(safety_config.snapshot_text())  # the safety configuration it ran under
    assert row[7].startswith("audit/") and row[8] == "time_limit"
    events = [event for (event,) in conn.execute("SELECT event FROM audit ORDER BY line")]
    assert events[0] == "session_opened" and events[-1] == "session_closed"
    assert any("listen-only confirmed" in line.lower() for line in lines)


def test_armed_mode_ends_a_session_after_60_s_of_silence_and_starts_another(root, sim, on_bus, lines, reader):
    sim.bus.call_at(1010.0005, sim.vehicle.key_off)
    sim.bus.call_at(1085.0005, sim.vehicle.key_on)
    result = run(root, sim, lines, seconds=95.0)
    conn = reader()
    [first, second] = sessions(conn)
    assert first[1:3] == ("closed", "bus_silent") and second[1:3] == ("closed", "time_limit")
    assert first[6] == "2026-09-26T18:00:10+00:00"  # it ended at its last frame, not when the silence ran out
    assert second[5].startswith("2026-09-26T18:01:25")  # the next started with the first frame after key on
    assert first[3] + second[3] == len(on_bus) == result.frames
    assert result.sessions == (first[0], second[0])


def test_a_pause_shorter_than_the_silence_limit_stays_one_session(root, sim, on_bus, lines, reader):
    sim.bus.call_at(1005.0005, sim.vehicle.key_off)
    sim.bus.call_at(1050.0005, sim.vehicle.key_on)
    run(root, sim, lines, seconds=55.0)
    [(_, _, _, frames, *_)] = sessions(reader())
    assert frames == len(on_bus)


def test_a_bus_that_never_wakes_opens_no_session(root, sim, lines, reader):
    sim.vehicle.key_off()
    result = run(root, sim, lines, seconds=5.0)
    assert result.sessions == () and result.frames == 0
    assert sessions(reader()) == []
    assert any("waiting for traffic" in line.lower() for line in lines)


@pytest.mark.parametrize(("signum", "reason"), [(signal.SIGINT, "ctrl_c"), (signal.SIGBREAK, "ctrl_break")])
def test_ctrl_c_and_ctrl_break_stop_cleanly(root, sim, on_bus, lines, reader, signum, reason):
    before = signal.getsignal(signum)
    sim.bus.call_at(1003.0005, lambda: signal.raise_signal(signum))
    result = run(root, sim, lines, seconds=10.0)
    assert result.end_reason == reason
    [(_, state, end_reason, frames, *_)] = sessions(reader())
    assert (state, end_reason, frames) == ("closed", reason, len(on_bus))
    assert sim.clock.monotonic() < 1004.0  # it stopped at once
    assert signal.getsignal(signum) is before  # and put the previous handler back


def test_the_stop_file_stops_cleanly(root, sim, on_bus, lines, reader):
    sim.bus.call_at(1003.0005, root.stop_file.touch)
    result = run(root, sim, lines, seconds=10.0)
    assert result.end_reason == "stop_file"
    assert sessions(reader())[0][3] == len(on_bus)
    assert sim.clock.monotonic() < 1005.0  # it's checked every second
    assert not root.stop_file.exists()  # the request was taken


def test_a_stop_file_left_from_before_does_not_stop_the_next_capture(root, sim, lines):
    root.ensure()
    root.stop_file.touch()
    assert run(root, sim, lines, seconds=3.0).end_reason == "time_limit"


def test_a_second_capture_is_refused_while_one_runs(root, sim, lines):
    root.ensure()
    with CaptureLock.take(root), pytest.raises(CaptureRunning):
        run(root, sim, lines)
    assert not root.capture_db.exists()  # it touched nothing


def test_recovery_runs_before_the_capture(root, sim, lines, reader):
    first = run(root, sim, lines, seconds=2.0)
    conn = capture_db.open_capture(root)
    try:
        conn.execute("UPDATE sessions SET state = 'open', ended_utc = NULL, end_reason = NULL")
        conn.execute("UPDATE runs SET ended_utc = NULL, end_reason = NULL")
    finally:
        conn.close()
    result = run(root, sim, lines, seconds=2.0)
    assert [session.id for session in result.recovery.sessions] == list(first.sessions)
    assert result.recovery.runs == (first.run_id,)
    assert [row[1:3] for row in sessions(reader())] == [("recovered", "interrupted"), ("closed", "time_limit")]


def test_the_live_feed_carries_the_status_and_a_heartbeat(root, sim, lines):
    seen = []
    sim.bus.call_at(1005.5, lambda: seen.append(read_live(root)))
    result = run(root, sim, lines, seconds=8.0)
    [status] = seen
    assert (status.run_id, status.state, status.interface) == (result.run_id, "capturing", "simulator")
    assert status.session_id == result.sessions[0] and status.frames > 1200
    assert abs(status.frames_per_second - 300) <= 3
    assert status.heartbeat_utc.startswith("2026-09-26T18:00:05")  # the tick at 5 s, give or take a poll
    after = read_live(root)
    assert (after.state, after.session_id) == ("stopped", None)


def test_channel_status_becomes_session_and_run_events(root, sim, lines, reader):
    """A status while a session runs is the session's; one while the bus is quiet belongs to the run."""
    sim.bus.call_at(1002.0005, lambda: sim.dll.inject_status(HANDLE, pc.PCAN_ERROR_BUSLIGHT))
    sim.bus.call_at(1003.0005, sim.vehicle.key_off)
    sim.bus.call_at(1070.0005, lambda: sim.dll.inject_status(HANDLE, pc.PCAN_ERROR_OK))  # the controller (re)activated
    run(root, sim, lines, seconds=72.0)
    conn = reader()
    rows = conn.execute("SELECT session IS NOT NULL, kind, detail FROM events ORDER BY id").fetchall()
    assert (1, "status", json.dumps({"status": f"0x{pc.PCAN_ERROR_BUSLIGHT:05X}"})) in rows
    assert (0, "status", json.dumps({"status": "0x00000"})) in rows
    # The listen-only recheck that status set off is in the audit log, and copied to the run's events.
    assert any(kind == "listen_only_rechecked" and not in_session for in_session, kind, _ in rows)


def test_losing_the_channel_ends_the_run_and_keeps_what_was_captured(root, sim, on_bus, lines, reader):
    sim.bus.call_at(1003.0005, lambda: sim.dll.unplug(HANDLE))
    result = run(root, sim, lines, seconds=60.0)
    assert result.end_reason == "channel_lost"
    [(_, state, reason, frames, *_)] = sessions(reader())
    assert (state, reason) == ("closed", "channel_lost")
    assert frames == sum(time < 1003.0005 for time in on_bus)  # every frame the adapter heard before it was unplugged
    assert any("could not reopen" in line for line in lines)
    assert sim.clock.monotonic() < 1015.0  # the safety core's few reopen attempts, then the run stopped


def test_keep_awake_is_held_on_the_main_thread_for_the_whole_capture(root, sim, on_bus, lines):
    calls: list[tuple[int, str, float, int]] = []

    def windows(flags: int) -> int:
        calls.append((flags, threading.current_thread().name, sim.clock.monotonic(), len(on_bus)))
        return ES_CONTINUOUS

    run(root, sim, lines, seconds=5.0, keep_awake=KeepAwake(windows))
    main = threading.main_thread().name
    assert calls == [
        (ES_CONTINUOUS | ES_SYSTEM_REQUIRED, main, 1000.0, 0),  # before the channel opened
        (ES_CONTINUOUS, main, pytest.approx(1005.0, abs=0.02), len(on_bus)),  # after the last frame
    ]


def test_a_channel_that_refuses_to_open_ends_the_run_with_why(root, sim, lines, reader):
    sim.dll.initialize_status = pc.PCAN_ERROR_ILLHW
    with pytest.raises(InterfaceError):
        run(root, sim, lines)
    conn = reader()
    assert conn.execute("SELECT end_reason FROM runs").fetchone() == ("refused: InterfaceError",)
    assert [event for (event,) in conn.execute("SELECT event FROM audit")] == ["rejected", "session_refused"]
    assert conn.execute("SELECT kind FROM events").fetchall() == [("rejected",)]  # copied to the run's events
    assert read_live(root).state == "stopped"
    assert not root.lock_file.exists() or not capture_running(root)  # the lock went with it


def test_a_live_feed_that_cannot_be_written_never_stops_the_capture(root, sim, on_bus, lines, reader):
    root.ensure()
    root.live_db.mkdir()  # SQLite can't open a folder
    result = run(root, sim, lines, seconds=3.0)
    assert result.frames == len(on_bus)
    conn = reader()
    [(kind, detail)] = conn.execute("SELECT kind, detail FROM events").fetchall()
    assert kind == "live_feed_failed" and "OperationalError" in detail
    assert any("live feed can't be written" in line for line in lines)


def test_a_refused_keep_awake_request_is_reported_and_the_capture_goes_on(root, sim, lines):
    result = run(root, sim, lines, seconds=2.0, keep_awake=KeepAwake(lambda flags: 0))
    assert result.end_reason == "time_limit"
    assert any("refused the keep-awake request" in line for line in lines)


def test_the_truck_is_the_real_driver_on_the_system_clock():
    """Described, not opened: nothing here loads a driver."""
    source = truck("PCAN_USBBUS1")
    assert source.live and source.library is None and source.interface == "pcan"
    assert type(source.clock) is SystemClock


def failing_from(real, first_failing_call: int, last_failing_call: int | None = None):
    """A storage call that fails from its nth call on (or only up to a last one), like a disk that fills or hiccups."""
    calls = []

    def stand_in(*args, **kwargs):
        calls.append(1)
        if len(calls) >= first_failing_call and (last_failing_call is None or len(calls) <= last_failing_call):
            raise sqlite3.OperationalError("disk I/O error")
        return real(*args, **kwargs)

    return stand_in


def storage_errors(conn) -> list[dict]:
    return [json.loads(detail) for (detail,) in conn.execute("SELECT detail FROM events WHERE kind = 'storage_error'")]


def test_a_storage_error_stops_the_run_and_loses_no_drained_frame(root, sim, on_bus, lines, reader, monkeypatch):
    """Review finding L8: the database fails from the fourth second on. Everything drained reaches the segment,
    the session is left open, and the next capture's recovery indexes all of it."""
    monkeypatch.setattr(capture_db, "record_second", failing_from(capture_db.record_second, 4))
    result = run(root, sim, lines, seconds=10.0)
    assert result.end_reason == "storage_error" and sim.clock.monotonic() < 1006.0
    assert result.frames == len(on_bus)
    conn = reader()
    [(session, state, *_)] = sessions(conn)
    assert state == "open"
    [error] = storage_errors(conn)
    assert error["sessions_left_open"] == [session] and error["frames_not_written"] == 0
    assert error["frames_not_indexed"] > 0 and "disk I/O error" in error["error"]
    assert conn.execute("SELECT end_reason FROM runs").fetchone() == ("storage_error",)
    assert any(line.startswith("Storage failed") for line in lines)
    monkeypatch.undo()
    writer = capture_db.open_capture(root)
    try:
        [recovered] = recover(writer, root, now_utc="2026-09-26T19:00:00+00:00").sessions
    finally:
        writer.close()
    assert recovered.seconds_indexed > 0
    assert sessions(reader())[0][1:4] == ("recovered", "interrupted", len(on_bus))  # every frame the bus carried


def test_a_storage_error_that_passes_is_retried_before_the_session_closes(root, sim, on_bus, lines, reader, monkeypatch):
    monkeypatch.setattr(capture_db, "record_second", failing_from(capture_db.record_second, 4, 4))
    result = run(root, sim, lines, seconds=10.0)
    assert result.end_reason == "storage_error"  # the run still stops: storage failed once
    conn = reader()
    assert sessions(conn)[0][1:4] == ("closed", "storage_error", len(on_bus))
    [error] = storage_errors(conn)
    assert error["sessions_left_open"] == [] and error["frames_not_written"] == 0


def test_the_channel_is_read_once_more_before_it_closes(root, sim, on_bus, reader):
    def report(line: str) -> None:
        if line.startswith("Stopping"):
            sim.dll.inject_frame(HANDLE, 0x123, b"\x01\x02")  # arrives after the loop's last read

    sim.bus.call_at(1003.0005, root.stop_file.touch)
    drive(root, simulated(sim), seconds=10.0, report=report)
    assert sessions(reader())[0][3] == len(on_bus) + 1


def test_a_storage_error_while_waiting_for_traffic_stops_the_run(root, sim, lines, reader, monkeypatch):
    real = capture_db.add_event

    def add_event(conn, **fields):
        if fields["kind"] == "status":
            raise sqlite3.OperationalError("disk I/O error")
        return real(conn, **fields)

    monkeypatch.setattr(capture_db, "add_event", add_event)
    sim.vehicle.key_off()
    sim.bus.call_at(1002.0005, lambda: sim.dll.inject_status(HANDLE, pc.PCAN_ERROR_BUSLIGHT))
    result = run(root, sim, lines, seconds=10.0)
    assert result.end_reason == "storage_error" and sim.clock.monotonic() < 1003.0
    [error] = storage_errors(reader())
    assert error["sessions_left_open"] == [] and error["events_not_written"] == 1


def test_frames_for_a_session_that_could_not_start_are_counted(root, sim, on_bus, lines, reader, monkeypatch):
    monkeypatch.setattr(capture_db, "open_session", failing_from(capture_db.open_session, 1))
    result = run(root, sim, lines, seconds=10.0)
    assert result.end_reason == "storage_error" and result.sessions == ()
    [error] = storage_errors(reader())
    assert error["frames_not_written"] == len(on_bus) == result.frames


def test_a_run_that_cannot_be_ended_is_left_for_the_next_capture(root, sim, lines, reader, monkeypatch):
    monkeypatch.setattr(capture_db, "end_run", failing_from(capture_db.end_run, 1))
    result = run(root, sim, lines, seconds=2.0)
    assert result.end_reason == "time_limit"  # the capture itself went fine
    assert any("couldn't be ended" in line for line in lines)
    assert reader().execute("SELECT ended_utc FROM runs").fetchone() == (None,)
    monkeypatch.undo()
    assert run(root, sim, lines, seconds=1.0).recovery.runs == (result.run_id,)


def test_a_storage_error_that_cannot_be_recorded_is_still_reported(root, sim, lines, monkeypatch):
    monkeypatch.setattr(capture_db, "add_event", failing_from(capture_db.add_event, 1))
    sim.vehicle.key_off()
    sim.bus.call_at(1002.0005, lambda: sim.dll.inject_status(HANDLE, pc.PCAN_ERROR_BUSLIGHT))
    assert run(root, sim, lines, seconds=10.0).end_reason == "storage_error"
    assert any("couldn't be recorded in the capture database either" in line for line in lines)
    assert any(line.startswith("Storage error: OperationalError: disk I/O error") for line in lines)


def test_a_channel_event_that_cannot_be_copied_stops_the_run_and_stays_in_the_audit_log(
    root, sim, lines, reader, monkeypatch
):
    real = capture_db.add_event

    def add_event(conn, **fields):
        if fields["kind"] == "listen_only_rechecked":
            raise sqlite3.OperationalError("disk I/O error")
        return real(conn, **fields)

    monkeypatch.setattr(capture_db, "add_event", add_event)
    sim.bus.call_at(1002.0005, lambda: sim.dll.inject_status(HANDLE, pc.PCAN_ERROR_OK))  # the controller (re)activated
    assert run(root, sim, lines, seconds=10.0).end_reason == "storage_error"
    events = [event for (event,) in reader().execute("SELECT event FROM audit")]
    assert "listen_only_rechecked" in events


def test_the_simulator_needs_a_time_limit(root, sim, lines):
    with pytest.raises(ValueError, match="seconds"):
        drive(root, simulated(sim), seconds=None, report=lines.append)


def test_keeps_up_with_2000_frames_a_second(root, sim, on_bus, lines, reader):
    """Ten simulated seconds of a bus carrying 2,000 frames a second, recorded faster than real time."""
    sim.vehicle.add_background_traffic(1700)
    started = time.perf_counter()
    result = run(root, sim, lines, seconds=10.0)
    elapsed = time.perf_counter() - started
    assert result.frames == len(on_bus) >= 20_000
    assert sessions(reader())[0][3] == len(on_bus)
    assert elapsed < 10.0, f"{elapsed:.1f} s of work for 10 s of traffic"
