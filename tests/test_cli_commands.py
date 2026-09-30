"""lasto drive and lasto log from the command line.

drive runs the simulator here, in a temporary data folder. Where a test gives --live, the operation is
replaced with a stand-in first, so no test ever reaches a driver: it checks only what the CLI asks for.
"""

from __future__ import annotations

import pytest

from lasto import cli
from lasto.capture.recovery import Recovery
from lasto.operations import drive as drive_operation
from lasto.operations.drive import DriveResult
from lasto.safety import pcan_constants as pc
from lasto.safety.errors import InterfaceError
from lasto.services import sessions
from lasto.sim.vehicle import build_sim
from lasto.storage.capture_lock import CaptureLock
from lasto.storage.live_db import LiveStatus
from lasto.storage.root import DataRoot


def run(*argv: str) -> int:
    return cli.main(list(argv))


@pytest.fixture
def data(tmp_path) -> str:
    return str(tmp_path / "data")


@pytest.fixture
def asked(monkeypatch) -> list[tuple]:
    """Stand-ins for the drive operation: they record what the CLI asked for, and open nothing."""
    calls: list[tuple] = []

    def fake_drive(root, source, *, seconds, report):
        calls.append(("drive", root.path, source, seconds))
        return DriveResult("run", ("session",), 12, "ctrl_c", Recovery((), ()))

    monkeypatch.setattr(drive_operation, "drive", fake_drive)
    monkeypatch.setattr(drive_operation, "truck", lambda channel: ("truck", channel))
    monkeypatch.setattr(drive_operation, "hold_refusals", lambda root: calls.append(("held", root.path)))
    return calls


def test_drive_runs_the_simulator_by_default(data, capsys):
    assert run("drive", "--seconds", "3", "--data", data) == 0
    out = capsys.readouterr().out
    assert "the simulator, 3 simulated seconds" in out
    assert "listen-only confirmed on PCAN_USBBUS1" in out and "Stopped (time_limit)" in out
    root = DataRoot(data)
    assert root.capture_db.exists()
    assert (root.audit_dir / "held.jsonl").exists()  # held refusals go to disk from the start (B2)


def test_the_simulator_runs_60_seconds_unless_told_otherwise(data, asked):
    assert run("drive", "--data", data) == 0
    [held, (_, root, source, seconds)] = asked
    assert held == ("held", DataRoot(data).path) and root == DataRoot(data).path
    assert source.interface == "simulator" and seconds == 60.0


@pytest.mark.parametrize(("extra", "seconds"), [((), None), (("--seconds", "90"), 90.0)])
def test_drive_live_asks_for_the_truck_on_the_channel_given(data, asked, capsys, extra, seconds):
    assert run("drive", "--live", "--channel", "PCAN_USBBUS2", "--data", data, *extra) == 0
    [held, (_, _, source, asked_seconds)] = asked
    assert held[0] == "held"  # before the capture
    assert source == ("truck", "PCAN_USBBUS2") and asked_seconds == seconds
    assert "Stop with Ctrl+C" in capsys.readouterr().out


def test_a_channel_the_safety_core_refuses_is_reported(data, monkeypatch, capsys):
    def refused(root, source, *, seconds, report):
        raise InterfaceError("PCAN_USBBUS1 isn't available")

    monkeypatch.setattr(drive_operation, "drive", refused)
    assert run("drive", "--seconds", "3", "--data", data) == 1
    assert "PCAN_USBBUS1 isn't available" in capsys.readouterr().err


def test_a_second_capture_is_refused(data, capsys):
    root = DataRoot(data)
    root.ensure()
    with CaptureLock.take(root):
        assert run("drive", "--seconds", "3", "--data", data) == 1
    assert "another capture is running" in capsys.readouterr().err


@pytest.mark.parametrize("reason", ["channel_lost", "storage_error"])
def test_a_run_that_lost_its_channel_or_its_storage_exits_with_an_error(data, monkeypatch, reason):
    def stopped(root, source, *, seconds, report):
        return DriveResult("run", ("session",), 12, reason, Recovery((), ()))

    monkeypatch.setattr(drive_operation, "drive", stopped)
    assert run("drive", "--seconds", "3", "--data", data) == 1


@pytest.mark.parametrize("command", ["drive", "log"])
def test_a_data_folder_that_names_a_device_is_refused(command, capsys):
    assert run(command, "--data", "NUL") == 1
    assert "names a device" in capsys.readouterr().err


def test_log_before_any_capture(data, capsys):
    assert run("log", "--data", data) == 0
    assert "No sessions captured yet" in capsys.readouterr().out


def test_log_lists_sessions_and_shows_one(data, capsys):
    run("drive", "--seconds", "3", "--data", data)
    capsys.readouterr()
    assert run("log", "--data", data) == 0
    listing = capsys.readouterr().out
    [session] = [summary.id for summary in sessions.list_sessions(DataRoot(data))]
    assert session in listing and "2026-09-26 18:00:00" in listing and "time_limit" in listing

    assert run("log", "last", "--data", data) == 0
    detail = capsys.readouterr().out
    assert f"Session {session}" in detail
    assert "PCAN-USB (simulated), PCAN-Basic 4.7.0.11" in detail
    assert "session_opened" in detail and "listen_only_confirmed=True" in detail

    assert run("log", session[:8], "--bus", "--data", data) == 0
    bus = capsys.readouterr().out
    assert "4 IDs" in bus
    assert "025" in bus and "0B4" in bus and "00 00 00 00 00 00 00 00" in bus


def test_log_shows_the_events_of_a_session_and_its_run(data, capsys):
    sim = build_sim()
    sim.bus.call_at(1001.0005, lambda: sim.dll.inject_status(pc.USB_CHANNELS[1], pc.PCAN_ERROR_BUSLIGHT))
    sim.bus.call_at(1002.0005, sim.vehicle.key_off)
    sim.bus.call_at(1070.0005, lambda: sim.dll.inject_status(pc.USB_CHANNELS[1], pc.PCAN_ERROR_BUSHEAVY))
    drive_operation.drive(DataRoot(data), drive_operation.simulated(sim), seconds=72.0, report=lambda line: None)
    assert run("log", "last", "--data", data) == 0
    out = capsys.readouterr().out
    assert "Events:" in out and "status  status=0x00004" in out
    assert "Run events, while no session was open:" in out and "status  status=0x00008" in out


def test_log_of_a_session_that_is_not_there(data, capsys):
    run("drive", "--seconds", "2", "--data", data)
    assert run("log", "zzz", "--data", data) == 1
    assert "no session matches 'zzz'" in capsys.readouterr().err


def test_log_says_when_a_capture_is_running(data, monkeypatch, capsys):
    status = LiveStatus(
        pid=4242,
        run_id="run",
        session_id="3f2a",
        state="capturing",
        interface="pcan",
        channel="PCAN_USBBUS1",
        started_utc="2026-09-26T18:00:00+00:00",
        heartbeat_utc="2026-09-26T18:05:00+00:00",
        frames=90_000,
        error_frames=0,
        frames_per_second=300,
        bus=None,
    )
    monkeypatch.setattr(sessions, "running_capture", lambda root: status)
    assert run("log", "--data", data) == 0
    out = capsys.readouterr().out
    assert "A capture is running on PCAN_USBBUS1" in out and "90,000 frames" in out and "300 a second" in out
