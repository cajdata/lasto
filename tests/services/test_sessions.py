"""Captured sessions for lasto log, read-only from the capture database (lasto.services.sessions)."""

from __future__ import annotations

import pytest

from lasto.operations.drive import DriveResult, drive, simulated
from lasto.services.sessions import (
    SessionNotFound,
    bus_stats,
    find_session,
    list_sessions,
    running_capture,
    session_detail,
)
from lasto.sim.vehicle import ENGINE_RPM_ID, STEERING_ANGLE_ID, VEHICLE_SPEED_ID, YAW_RATE_ID, build_sim
from lasto.storage import capture_db
from lasto.storage.capture_lock import CaptureLock
from lasto.storage.root import DataRoot


@pytest.fixture
def root(tmp_path) -> DataRoot:
    return DataRoot(tmp_path / "data")


def capture(root: DataRoot, seconds: float, setup=lambda sim: None) -> DriveResult:
    sim = build_sim()
    setup(sim)
    return drive(root, simulated(sim), seconds=seconds, report=lambda line: None)


def key_cycle(sim) -> None:
    """Ten seconds of traffic, key off past the silence limit, then traffic again."""
    sim.bus.call_at(1010.0005, sim.vehicle.key_off)
    sim.bus.call_at(1080.0005, sim.vehicle.key_on)


def test_nothing_captured_yet(root):
    assert list_sessions(root) == []
    with pytest.raises(SessionNotFound, match="no sessions"):
        find_session(root, "last")


def test_sessions_are_listed_newest_first_with_their_size(root):
    result = capture(root, 90.0, key_cycle)
    newest, oldest = list_sessions(root)
    assert (newest.id, oldest.id) == (result.sessions[1], result.sessions[0])
    assert (oldest.state, oldest.end_reason, oldest.ids, oldest.error_frames) == ("closed", "bus_silent", 4, 0)
    assert oldest.seconds == pytest.approx(10.0, abs=0.01)  # from its first frame to its last
    assert oldest.frames == pytest.approx(3000, abs=5)
    assert oldest.stored_bytes > 0
    assert oldest.mb_per_hour == pytest.approx(oldest.stored_bytes / 1e6 / (oldest.seconds / 3600))
    assert oldest.started_utc == "2026-09-26T18:00:00+00:00" and oldest.run_id == result.run_id


def test_a_session_is_found_by_last_by_its_id_or_by_its_first_characters(root):
    result = capture(root, 90.0, key_cycle)
    first, second = result.sessions
    assert find_session(root, "last").id == second
    assert find_session(root, first).id == first
    assert find_session(root, first[:8]).id == first


@pytest.mark.parametrize("which", ["", "zzz", "%", "_", "0000"])
def test_a_session_that_is_not_there_is_reported(root, which):
    capture(root, 2.0)
    with pytest.raises(SessionNotFound):
        find_session(root, which)


def test_a_prefix_two_sessions_share_is_ambiguous(root, monkeypatch):
    ids = iter(f"abcd{n:04x}-0000-4000-8000-000000000000" for n in range(100))
    monkeypatch.setattr(capture_db, "new_id", lambda: next(ids))
    first, second = capture(root, 90.0, key_cycle).sessions
    with pytest.raises(SessionNotFound, match="more than one"):
        find_session(root, "abcd")
    assert find_session(root, second[:8]).id == second
    with pytest.raises(SessionNotFound):
        find_session(root, "-")  # every ID has dashes, but not at the start


def test_bus_stats_per_id(root):
    def speed_up(sim) -> None:
        def fifty() -> None:
            sim.vehicle.state.speed_kph = 50.0

        sim.bus.call_at(1005.0005, fifty)

    result = capture(root, 10.0, speed_up)
    stats = {one.can_id: one for one in bus_stats(root, result.sessions[0])}
    assert set(stats) == {STEERING_ANGLE_ID, YAW_RATE_ID, VEHICLE_SPEED_ID, ENGINE_RPM_ID}
    steering = stats[STEERING_ANGLE_ID]
    assert abs(steering.frames - 1001) <= 1 and not steering.extended  # 100 a second, both ends counted
    assert steering.period_ms == pytest.approx(10.0, abs=0.01)
    assert steering.gap_min_ms == pytest.approx(10.0, abs=0.01) and steering.gap_max_ms == pytest.approx(10.0, abs=0.01)
    assert steering.rate_hz == pytest.approx(100.1, abs=0.2)  # frames over the session's 10 s
    assert (steering.dlc_min, steering.dlc_max) == (8, 8)
    assert steering.changed_bits == bytes(8)  # stuck at its invalid maximum, like the truck's
    assert steering.first_s == 0.0 and steering.last_s == pytest.approx(10.0, abs=0.01)
    # 50 km/h is 5000 = 0x1388 in bytes 5 and 6 of the speed frame: exactly those bits changed.
    assert stats[VEHICLE_SPEED_ID].changed_bits == bytes([0, 0, 0, 0, 0, 0x13, 0x88, 0])


def test_the_detail_of_a_session(root):
    def status(sim) -> None:
        from lasto.safety import pcan_constants as pc

        sim.bus.call_at(1002.0005, lambda: sim.dll.inject_status(pc.USB_CHANNELS[1], pc.PCAN_ERROR_BUSLIGHT))

    result = capture(root, 5.0, status)
    detail = session_detail(root, result.sessions[0])
    assert detail.summary.id == result.sessions[0]
    assert (detail.run.interface, detail.run.channel, detail.run.hardware) == (
        "simulator",
        "PCAN_USBBUS1",
        "PCAN-USB (simulated)",
    )
    assert (detail.run.end_reason, detail.run.api_version) == ("time_limit", "4.7.0.11")
    [event] = detail.events
    assert (event.kind, event.detail) == ("status", {"status": "0x00004"})
    assert [entry.event for entry in detail.audit] == ["session_opened", "session_closed"]
    assert detail.audit[0].record["listen_only_confirmed"] is True
    assert detail.run_events == ()


@pytest.mark.parametrize("text", ["not json", "[1, 2]"])
def test_an_unreadable_audit_line_is_shown_as_its_text(root, text):
    result = capture(root, 2.0)
    conn = capture_db.open_capture(root)
    try:
        capture_db.add_audit_lines(conn, [capture_db.AuditLine(result.run_id, 99, "", capture_db.UNREADABLE, text)])
    finally:
        conn.close()
    last = session_detail(root, "last").audit[-1]
    assert (last.event, last.record) == (capture_db.UNREADABLE, {"text": text})


def test_a_running_capture_is_reported_with_its_live_status(root):
    seen = []

    def watch(sim) -> None:
        sim.bus.call_at(1003.5, lambda: seen.append(running_capture(root)))

    result = capture(root, 5.0, watch)
    [status] = seen
    assert (status.run_id, status.state) == (result.run_id, "capturing")
    assert running_capture(root) is None  # the lock is gone once the capture ends


def test_a_live_feed_without_a_capture_holding_the_lock_is_not_a_running_capture(root):
    capture(root, 2.0)
    with CaptureLock.take(root):
        pass
    assert running_capture(root) is None
