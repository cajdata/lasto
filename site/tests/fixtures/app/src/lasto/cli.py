"""Frozen test fixture, not the app: what sitegen reads from src/lasto/cli.py (the command table,
the command sets, the simulator default, and build_parser), copied from the app at dab6e21 (Phase 2
as approved), and nothing else. See site/tests/fixtures/README.md.
"""

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
SIMULATED_SECONDS = 60.0


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
