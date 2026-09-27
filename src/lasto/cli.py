"""Command-line entry point: lasto COMMAND.

The simulator is the default. Real hardware needs --live plus an explicit
--channel (PCAN) or --port (OBDLink), typed every time. Every parser turns
off option abbreviation, so nothing shorter than --live can turn it on.
"""

from __future__ import annotations

import argparse
import re

from lasto import __version__

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
}
HARDWARE_COMMANDS = frozenset({"drive", "map", "snapshot", "identify", "discover"})

_CHANNEL = re.compile(r"PCAN_USBBUS([1-9]|1[0-6])")
_PORT = re.compile(r"COM[1-9][0-9]{0,2}")


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
        if name == "drive":
            sub.add_argument("--profile", help="polled logging profile (default: passive capture)")
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in HARDWARE_COMMANDS:
        _check_hardware_arguments(parser, args)
    _help, phase = COMMANDS[args.command]
    mode = "real hardware" if getattr(args, "live", False) else "the simulator"
    print(f"lasto {__version__}: '{args.command}' with {mode} arrives in Phase {phase}. Nothing is captured yet.")
    return 0
