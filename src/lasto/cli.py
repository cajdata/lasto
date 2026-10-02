"""Command-line entry point: lasto COMMAND.

The simulator is the default. Real hardware needs --live plus an explicit
--channel (PCAN) or --port (OBDLink), typed every time. Every parser turns
off option abbreviation, so nothing shorter than --live can turn it on.

This module parses arguments and renders results; the work lives in
lasto.services and lasto.operations, imported only by the command that
needs them. Before it dispatches any command except gui, main() imports the
safety core, which installs the serial guard, so no lasto code in a
command's process can open a serial port but the STN link. The GUI process
never imports the safety core and installs its own guard
(docs/architecture.md §14.4).
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from lasto import __version__

if TYPE_CHECKING:
    from lasto.services.data import DataRoot
    from lasto.services.sessions import IdStats

# command: (help, phase that implements it)
COMMANDS = {
    "drive": ("Capture a drive: passive by default, polled with --profile", 2),
    "map": ("Map Creader conversations to signals", 3),
    "snapshot": ("Parked health snapshot: DTCs, freeze frames, readiness, Mode 06", 4),
    "identify": ("Read VIN, calibration IDs, CVNs, and ECU names", 4),
    "log": ("List sessions and the audit log", 2),
    "view": ("Live dashboard", 4),
    "discover": ("Parked discovery of identifiers the Creader never requests", 6),
    "decode": ("Re-decode a session with the current definitions", 5),
    "report": ("Session report", 8),
    "export": ("Export pack: CSV, Parquet, and summary JSON", 8),
    "verify": ("Confirm solver candidates as verified definitions", 3),
    "gui": ("Local web GUI for reviewing drives, mapping, and definitions", 9),
}
BUILT = frozenset({"drive", "log"})
HARDWARE_COMMANDS = frozenset({"drive", "map", "snapshot", "identify", "discover"})
# Commands whose process must never import the safety core (docs/architecture.md §14.4).
SAFETY_CORE_FREE_COMMANDS = frozenset({"gui"})
SIMULATED_SECONDS = 60.0
MAX_CAN_ID = 0x1FFFFFFF  # 29 bits, an extended ID
MAX_STANDARD_ID = 0x7FF

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_CHANNEL = re.compile(r"PCAN_USBBUS([1-9]|1[0-6])")
_PORT = re.compile(r"COM[1-9][0-9]{0,2}")


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of seconds: {text!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"seconds must be more than 0, not {text!r}")
    return value


def _offset(text: str) -> float:
    """Seconds into a session: 0 or more."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of seconds: {text!r}") from None
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f"seconds into a session are 0 or more, not {text!r}")
    return value


def _can_id(text: str) -> int:
    """A CAN ID in hex, with or without 0x: 025, 0x7E8, 18DAF110."""
    digits = text[2:] if text.lower().startswith("0x") else text
    if not re.fullmatch(r"[0-9A-Fa-f]{1,8}", digits) or int(digits, 16) > MAX_CAN_ID:
        raise argparse.ArgumentTypeError(f"not a CAN ID in hex (up to 1FFFFFFF): {text!r}")
    return int(digits, 16)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lasto", description="Read-only data logger for a 2006 Lexus GX470.", allow_abbrev=False
    )
    parser.add_argument("--version", action="version", version=f"lasto {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name, (help_text, _phase) in COMMANDS.items():
        sub = subparsers.add_parser(name, help=help_text, description=help_text, allow_abbrev=False)
        if name in HARDWARE_COMMANDS:
            sub.add_argument(
                "--live", action="store_true", help="use real hardware instead of the simulator (needs --channel or --port)"
            )
            sub.add_argument("--channel", help="PCAN channel such as PCAN_USBBUS1 (only with --live)")
            sub.add_argument("--port", help="OBDLink COM port such as COM5 (only with --live)")
        if name in BUILT:
            sub.add_argument("--data", metavar="DIR", help="data folder (default: LASTO_DATA, then %%LOCALAPPDATA%%\\lasto)")
        if name == "drive":
            sub.add_argument("--profile", help="polled logging profile (default: passive capture)")
            sub.add_argument(
                "--seconds",
                type=_seconds,
                help=f"stop after this long: simulated seconds in the simulator (default {SIMULATED_SECONDS:g}),"
                " an optional time limit on the truck",
            )
        if name == "log":
            sub.add_argument("session", nargs="?", help="a session: its ID, its first characters, or 'last'")
            sub.add_argument("--bus", action="store_true", help="every CAN ID in the session, and which bits changed")
            sub.add_argument(
                "--id", type=_can_id, metavar="HEX", help="one CAN ID's raw frames over time, such as 025 (read-only)"
            )
            sub.add_argument("--from", dest="start", type=_offset, metavar="S", help="with --id: from this many seconds in")
            sub.add_argument("--to", dest="end", type=_offset, metavar="S", help="with --id: up to this many seconds in")
    return parser


def _check_hardware_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not args.live:
        if args.channel or args.port:
            parser.error("--channel and --port need --live; without --live, lasto uses the simulator")
        return
    if not (args.channel or args.port):
        parser.error("--live needs --channel PCAN_USBBUSn or --port COMn")
    if args.channel and not _CHANNEL.fullmatch(args.channel):
        parser.error(f"--channel must be PCAN_USBBUS1 to PCAN_USBBUS16, not {args.channel!r}")
    if args.port and not _PORT.fullmatch(args.port):
        parser.error(f"--port must look like COM5, not {args.port!r}")


def _check_log_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.id is None:
        if args.start is not None or args.end is not None:
            parser.error("--from and --to choose part of an --id view; give --id too")
        return
    if args.bus:
        parser.error("--id and --bus are separate views; give one of them")
    if args.start is not None and args.end is not None and args.end < args.start:
        parser.error("--to can't come before --from")


def _check_drive_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.port:
        parser.error("drive captures from the PCAN-USB (--channel) for now; the OBDLink MX+ (--port) comes in a later phase")
    if args.profile:
        parser.error("polled logging profiles (--profile) come in Phase 4; drive is passive until then")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command not in SAFETY_CORE_FREE_COMMANDS:
        # Before anything else runs: importing the safety core installs its serial guard (finding P2).
        import lasto.safety  # noqa: F401
    if args.command in HARDWARE_COMMANDS:
        _check_hardware_arguments(parser, args)
    if args.command == "drive":
        _check_drive_arguments(parser, args)
    if args.command == "log":
        _check_log_arguments(parser, args)
    return _dispatch(args)


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "drive":
        return _drive(args)
    if args.command == "log":
        return _log(args)
    _help, phase = COMMANDS[args.command]
    mode = "real hardware" if getattr(args, "live", False) else "the simulator"
    print(f"lasto {__version__}: '{args.command}' with {mode} arrives in Phase {phase}. Nothing is captured yet.")
    return 0


def _fail(message: str) -> int:
    print(f"lasto: {message}", file=sys.stderr)
    return 1


# ---- drive ----


def _drive(args: argparse.Namespace) -> int:
    from lasto.operations import data_folder
    from lasto.operations import drive as capture
    from lasto.services.data import data_root

    try:
        root = data_root(args.data)
    except ValueError as exc:
        return _fail(str(exc))
    data_folder.use_data_folder(root)
    if args.live:
        source = capture.truck(args.channel)
        seconds = args.seconds
        limit = "" if seconds is None else f", stopping after {seconds:g} s"
        print(f"lasto drive: {args.channel}, listen-only{limit}. Stop with Ctrl+C or Ctrl+Break. Data: {root.path}")
    else:
        source = capture.simulated()
        seconds = SIMULATED_SECONDS if args.seconds is None else args.seconds
        print(f"lasto drive: the simulator, {seconds:g} simulated seconds. Data: {root.path}")
    try:
        result = capture.drive(root, source, seconds=seconds, report=print)
    except capture.CANNOT_START as exc:
        return _fail(str(exc))
    return 1 if result.end_reason in ("channel_lost", "storage_error") else 0


# ---- log ----


def _when(utc: str | None) -> str:
    return "-" if not utc else datetime.fromisoformat(utc).strftime("%Y-%m-%d %H:%M:%S")


def _length(seconds: float) -> str:
    hours, rest = divmod(round(seconds), 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}"


def _mb(stored_bytes: int) -> str:
    return f"{stored_bytes / 1e6:.2f} MB"


def _fields(record: dict[str, object]) -> str:
    """A record's fields as key=value on one line: line breaks inside a value (PCAN-Basic's channel version has
    them) become single spaces."""
    return " ".join(
        f"{key}={' '.join(str(value).split())}" for key, value in record.items() if key not in ("event", "utc", "mono")
    )


def _log(args: argparse.Namespace) -> int:
    from lasto.operations import data_folder
    from lasto.services import sessions
    from lasto.services.data import data_root

    try:
        root = data_root(args.data)
    except ValueError as exc:
        return _fail(str(exc))
    data_folder.read_data_folder(root)
    running = sessions.running_capture(root)
    if running is not None:
        print(
            f"A capture is running on {running.channel} ({running.interface}): {running.state},"
            f" {running.frames:,} frames in this session, {running.frames_per_second:,} a second."
            f" Last heartbeat {_when(running.heartbeat_utc)} UTC."
        )
    try:
        if args.id is not None:
            return _show_trace(root, args.session or "last", args.id, start=args.start, end=args.end)
        if args.session is None and not args.bus:
            return _list_sessions(root)
        return _show_session(root, args.session or "last", bus=args.bus)
    except sessions.SessionNotFound as exc:
        return _fail(str(exc.args[0]))


def _list_sessions(root: DataRoot) -> int:
    from lasto.services import sessions

    found = sessions.list_sessions(root)
    if not found:
        print(f"No sessions captured yet in {root.path}.")
        return 0
    print(f"Sessions in {root.path}, newest first (times in UTC):")
    print(f"  {'Started':19}  {'Length':>8}  {'State':9}  {'End':12}  {'Frames':>10}  {'IDs':>4}  {'Stored':>9}  {'MB/h':>6}  Session")
    for one in found:
        rate = "-" if one.mb_per_hour is None else f"{one.mb_per_hour:.1f}"
        print(
            f"  {_when(one.started_utc):19}  {_length(one.seconds):>8}  {one.state:9}  {one.end_reason or '-':12}"
            f"  {one.frames:>10,}  {one.ids:>4}  {_mb(one.stored_bytes):>9}  {rate:>6}  {one.id}"
        )
    return 0


def _show_session(root: DataRoot, which: str, *, bus: bool) -> int:
    from lasto.services import sessions

    detail = sessions.session_detail(root, which)
    one, run = detail.summary, detail.run
    adapter = f"; {run.hardware}, PCAN-Basic {run.api_version}" if run.hardware else ""
    rate = "" if one.mb_per_hour is None else f", {one.mb_per_hour:.1f} MB per hour"
    print(f"Session {one.id}")
    print(f"  Run      {run.id} ({run.interface}, {run.channel}{adapter})")
    print(f"  Started  {_when(one.started_utc)} UTC")
    print(f"  Ended    {_when(one.ended_utc)} UTC, {one.end_reason or 'still open'} ({one.state})")
    print(f"  Length   {_length(one.seconds)}")
    print(f"  Frames   {one.frames:,} ({one.error_frames:,} error frames) from {one.ids} IDs")
    print(f"  Stored   {_mb(one.stored_bytes)}{rate}")
    if bus:
        _show_bus(sessions.bus_stats(root, one.id))
    for title, events in (("Events", detail.events), ("Run events, while no session was open", detail.run_events)):
        if events:
            print(f"{title}:")
            for event in events:
                print(f"  {_when(event.host_utc)}  {event.kind}  {_fields(event.detail)}")
    print(f"Audit log of run {run.id}:")
    for entry in detail.audit:
        print(f"  {_when(entry.utc) if entry.utc else '?':19}  {entry.event}  {_fields(entry.record)}")
    return 0


def _id_text(can_id: int) -> str:
    return f"{can_id:03X}" if can_id <= MAX_STANDARD_ID else f"{can_id:08X}"


def _time_of_day(utc_us: int) -> str:
    """A UTC time from whole microseconds, exactly (a float epoch would lose the last digits)."""
    return (_EPOCH + timedelta(microseconds=utc_us)).strftime("%H:%M:%S.%f")


def _show_trace(root: DataRoot, which: str, can_id: int, *, start: float | None, end: float | None) -> int:
    from lasto.services import sessions

    trace = sessions.id_trace(root, which, can_id, start_s=start, end_s=end)
    name = _id_text(can_id)
    span = "" if start is None and end is None else f" between {start or 0:g} s and {'the end' if end is None else f'{end:g} s'}"
    if not trace.frames:
        print(f"No frames with ID {name} in session {trace.session.id}{span}.")
    else:
        first, last = trace.frames[0].time_s, trace.frames[-1].time_s
        print(
            f"ID {name} in session {trace.session.id}: {len(trace.frames):,} frames, {first:.3f} s to {last:.3f} s"
            " after its first frame, by the adapter's clock"
        )
        print(f"  {'Time s':>12}  {'UTC':15}  {'DLC':>3}  Data")
        for frame in trace.frames:
            data = "R" if frame.rtr else " ".join(f"{byte:02X}" for byte in frame.data)
            print(f"  {frame.time_s:12.6f}  {_time_of_day(frame.utc_us)}  {len(frame.data):>3}  {data}")
    if trace.unreadable_seconds:
        count = len(trace.unreadable_seconds)
        seconds = ", ".join(str(second) for second in trace.unreadable_seconds)
        what = "1 second" if count == 1 else f"{count} seconds"
        print(f"Missing: {what} of the session couldn't be read from its segment (second {seconds}).")
    return 0


def _show_bus(stats: list[IdStats]) -> None:
    print(f"Bus: {len(stats)} IDs")
    print(
        f"  {'ID':8}  {'Frames':>9}  {'Rate/s':>7}  {'Period ms':>9}  {'Gap ms min-max':>15}  {'DLC':>3}"
        f"  {'Changed bits, bytes 0-7':23}  {'First s':>8}  {'Last s':>8}"
    )
    for one in stats:
        can_id = f"{one.can_id:08X}" if one.extended else f"{one.can_id:03X}"
        rate = "-" if one.rate_hz is None else f"{one.rate_hz:.1f}"
        period = "-" if one.period_ms is None else f"{one.period_ms:.2f}"
        gaps = "-" if one.gap_min_ms is None else f"{one.gap_min_ms:.2f}-{one.gap_max_ms:.2f}"
        dlc = f"{one.dlc_min}" if one.dlc_min == one.dlc_max else f"{one.dlc_min}-{one.dlc_max}"
        changed = " ".join(f"{byte:02X}" for byte in one.changed_bits)
        print(
            f"  {can_id:8}  {one.frames:>9,}  {rate:>7}  {period:>9}  {gaps:>15}  {dlc:>3}"
            f"  {changed:23}  {one.first_s:>8.3f}  {one.last_s:>8.3f}"
        )
