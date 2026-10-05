"""lasto drive, lasto log and lasto adapter from the command line.

drive and adapter run the simulator here, in a temporary data folder. Where a test gives --live, the operation is
replaced with a stand-in first, so no test ever reaches a driver: it checks only what the CLI asks for.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from lasto import cli
from lasto.capture.recovery import Recovery
from lasto.operations import adapter as adapter_operation
from lasto.operations import data_folder
from lasto.operations import drive as drive_operation
from lasto.operations.drive import DriveResult
from lasto.safety import pcan_constants as pc
from lasto.safety.errors import InterfaceError
from lasto.services import sessions
from lasto.sim.vehicle import build_sim
from lasto.storage import capture_db
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
    monkeypatch.setattr(data_folder, "use_data_folder", lambda root: calls.append(("held", root.path)))
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


@pytest.mark.parametrize("command", ["drive", "log", "adapter"])
def test_a_data_folder_that_names_a_device_is_refused(command, capsys):
    assert run(command, "--data", "NUL") == 1
    assert "names a device" in capsys.readouterr().err


def lasto_command(*argv: str) -> subprocess.CompletedProcess[str]:
    """lasto in a process of its own, outside the test run: guard v2 with no test allowances."""
    return subprocess.run([sys.executable, "-m", "lasto", *argv], capture_output=True, text=True, timeout=120)


def test_drive_then_log_in_processes_of_their_own(tmp_path):
    """Each command registers its data folder before it writes or reads, so both work under the strict guard."""
    data = str(tmp_path / "data")
    driven = lasto_command("drive", "--seconds", "2", "--data", data)
    assert driven.returncode == 0, driven.stderr
    assert "Stopped (time_limit): 1 session" in driven.stdout
    logged = lasto_command("log", "last", "--data", data)
    assert logged.returncode == 0, logged.stderr
    assert "listen_only_confirmed=True" in logged.stdout


def test_log_of_a_data_folder_that_does_not_exist_creates_nothing(tmp_path):
    missing = tmp_path / "nothing-here"
    logged = lasto_command("log", "--data", str(missing))
    assert logged.returncode == 0, logged.stderr
    assert "No sessions captured yet" in logged.stdout and not missing.exists()


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


def test_a_field_with_line_breaks_prints_on_one_line():
    """PCAN-Basic's channel version has line breaks and PEAK's copyright in it (step D's first live run)."""
    record = {
        "event": "session_opened",
        "utc": "2026-10-01T00:27:36+00:00",
        "mono": 1.0,
        "channel_version": "PCAN_USB 5.1.3.20113\n(KMDF 1.15, x64)\r\nCopyright (C) 1995-2026 by\nPEAK-System Technik GmbH, Darmstadt",
        "mode": "passive",
    }
    assert cli._fields(record) == (
        "channel_version=PCAN_USB 5.1.3.20113 (KMDF 1.15, x64) Copyright (C) 1995-2026 by PEAK-System Technik GmbH,"
        " Darmstadt mode=passive"
    )


def frame_lines(out: str) -> list[str]:
    """The lines of an --id trace that are frames: they start with a time."""
    return [line for line in out.splitlines() if line.strip()[:1].isdigit()]


def test_log_prints_one_ids_raw_bytes_over_time(data, capsys):
    sim = build_sim()

    def fifty() -> None:
        sim.vehicle.state.speed_kph = 50.0

    sim.bus.call_at(1002.0005, fifty)
    drive_operation.drive(DataRoot(data), drive_operation.simulated(sim), seconds=4.0, report=lambda line: None)
    assert run("log", "last", "--id", "0B4", "--data", data) == 0
    out = capsys.readouterr().out
    assert "ID 0B4 in session" in out
    frames = frame_lines(out)
    [stats] = [one for one in sessions.bus_stats(DataRoot(data), "last") if one.can_id == 0x0B4]
    assert len(frames) == stats.frames >= 200  # every one: 50 a second for 4 s
    assert frames[0].split() == ["0.004000", "18:00:00.004000", "8", "00", "00", "00", "00", "00", "00", "00", "00"]
    assert frames[-1].split()[3:] == ["00", "00", "00", "00", "00", "13", "88", "00"]  # 50 km/h


def test_log_prints_a_time_range_of_one_id(data, capsys):
    run("drive", "--seconds", "5", "--data", data)
    capsys.readouterr()
    assert run("log", "--id", "0x0b4", "--from", "1", "--to", "2", "--data", data) == 0  # the last session
    frames = frame_lines(capsys.readouterr().out)
    assert len(frames) == 50 and all(1.0 <= float(line.split()[0]) <= 2.0 for line in frames)


def test_log_says_which_seconds_of_an_id_could_not_be_read(data, capsys):
    run("drive", "--seconds", "3", "--data", data)
    root = DataRoot(data)
    conn = capture_db.read_capture(root)
    try:
        [(path,)] = conn.execute("SELECT path FROM segments").fetchall()
        offset, length = conn.execute("SELECT byte_offset, byte_length FROM seconds WHERE second = 1").fetchone()
    finally:
        conn.close()
    damaged = bytearray(root.absolute(path).read_bytes())
    damaged[offset + length - 2] ^= 0xFF
    root.absolute(path).write_bytes(bytes(damaged))
    capsys.readouterr()
    assert run("log", "last", "--id", "025", "--data", data) == 0
    out = capsys.readouterr().out
    assert "Missing: 1 second of the session couldn't be read from its segment (second 1)." in out
    assert frame_lines(out)  # the other seconds still print


def test_log_says_when_an_id_never_appeared(data, capsys):
    run("drive", "--seconds", "2", "--data", data)
    capsys.readouterr()
    assert run("log", "last", "--id", "7E8", "--data", data) == 0
    assert "No frames with ID 7E8 in session" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["--id", "zz"],
        ["--id", "20000000"],  # more than 29 bits
        ["--id", ""],
        ["--from", "1"],  # a time range needs --id
        ["--id", "025", "--to", "-1"],
        ["--id", "025", "--from", "5", "--to", "2"],
        ["--id", "025", "--bus"],
        ["--id", "025", "--fro", "1"],  # no abbreviations
    ],
)
def test_log_refuses_a_bad_id_or_range(data, argv):
    with pytest.raises(SystemExit) as exited:
        run("log", "last", *argv, "--data", data)
    assert exited.value.code == 2


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


# ---- adapter (bench test B3) ----


def test_adapter_checks_the_simulated_mx_plus_by_default(data, capsys):
    assert run("adapter", "--data", data) == 0
    out = capsys.readouterr().out
    assert "the simulated OBDLink MX+" in out
    assert "ELM327 v1.4b" in out and "OBDLink MX+ r3.2.1" in out and "12.63 V" in out
    assert "Stopped (done)" in out
    assert (DataRoot(data).audit_dir / "held.jsonl").exists()  # the data folder is set up as for a drive


def test_adapter_monitors_in_the_simulator(data, capsys):
    assert run("adapter", "--monitor", "can", "--seconds", "5", "--data", data) == 0
    out = capsys.readouterr().out
    assert "CAN, silently" in out and "Stopped (time_limit)" in out


@pytest.fixture
def adapter_asked(monkeypatch) -> list[tuple]:
    """Stand-ins for the adapter operation: they record what the CLI asked for, and open nothing."""
    calls: list[tuple] = []

    def fake_check(root, source, *, monitor, seconds, report):
        calls.append(("check", root.path, source, monitor, seconds))
        return adapter_operation.AdapterResult("ctrl_c", 2.5, "ELM327 v1.4b", {}, 12.6, 40)

    monkeypatch.setattr(adapter_operation, "check", fake_check)
    monkeypatch.setattr(adapter_operation, "bench", lambda port: ("bench", port))
    monkeypatch.setattr(data_folder, "use_data_folder", lambda root: calls.append(("held", root.path)))
    return calls


@pytest.mark.parametrize(("extra", "monitor", "seconds"), [((), None, None), (("--monitor", "kline", "--seconds", "30"), "kline", 30.0)])
def test_adapter_live_asks_for_the_port_given(data, adapter_asked, capsys, extra, monitor, seconds):
    assert run("adapter", "--live", "--port", "COM7", "--data", data, *extra) == 0
    [held, (_, _, source, asked_monitor, asked_seconds)] = adapter_asked
    assert held[0] == "held"
    assert source == ("bench", "COM7") and asked_monitor == monitor and asked_seconds == seconds
    assert "Power-cycle the MX+ first" in capsys.readouterr().out


def test_an_adapter_check_that_failed_exits_with_an_error(data, monkeypatch, capsys):
    def failed(root, source, *, monitor, seconds, report):
        report("the link to the adapter failed; it's closed, and nothing retries it")
        return adapter_operation.AdapterResult("adapter_error", 3.0, "", {}, None, 0)

    monkeypatch.setattr(adapter_operation, "check", failed)
    assert run("adapter", "--data", data) == 1


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (("adapter", "--seconds", "5"), "--seconds needs --monitor"),
        (("adapter", "--live", "--channel", "PCAN_USBBUS1"), "adapter checks the OBDLink MX+"),
        (("adapter", "--live", "--channel", "PCAN_USBBUS1", "--port", "COM5"), "adapter checks the OBDLink MX+"),
        (("adapter", "--monitor", "obd"), "invalid choice"),
        (("adapter", "--port", "COM5"), "need --live"),
    ],
)
def test_adapter_refuses_arguments_it_cant_use(argv, message, capsys):
    with pytest.raises(SystemExit) as exited:
        run(*argv)
    assert exited.value.code == 2 and message in capsys.readouterr().err
