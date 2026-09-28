"""Allowlist for OBDLink (STN) adapter commands (rule 4). Constants and pure checks.

The adapter's parser ignores case and spaces, lets a backspace edit its
buffer, sends any hex-only line to the vehicle as a request, and repeats the
last command on a bare carriage return. So a command is accepted only if it
uses a small set of printable characters, isn't hex-only, and after removing
spaces and uppercasing it exactly matches an entry below. The canonical form
is what gets sent, so what was checked is exactly what the adapter receives.

Nothing here lets the adapter put anything on the vehicle bus: no requests,
no header changes, no bus initialization, no keep-alives, no periodic
messages, no silent-mode defeat, and no writes to the adapter's non-volatile
memory. Source: OBDLink Family Reference and Programming Manual, Rev F.
"""

from __future__ import annotations

import re
import string
from typing import NoReturn

from lasto.safety._frozen import freeze
from lasto.safety.audit import refuse
from lasto.safety.errors import SafetyViolation

ALLOWED_INPUT = frozenset(string.ascii_letters + string.digits + " ,")
_HEX_DIGITS = frozenset("0123456789ABCDEF")

# Accepted only through StnAdapter.reset(), which then waits for the prompt before sending anything.
RESET_COMMANDS = frozenset({"ATZ", "ATWS"})

EXACT_COMMANDS = frozenset(
    {
        # output format
        "ATE0",
        "ATL0",
        "ATS0",
        "ATH1",
        "ATD0",
        "ATD1",
        "ATCAF0",
        "ATCAF1",
        # don't memorize the protocol
        "ATM0",
        # fixed protocols: raw 11-bit CAN 500k, ISO 15765 11-bit 500k, and K-line without autoinit
        "STP31",
        "STP33",
        "STP21",
        "STP23",
        # status reads
        "STPR",
        "STPRS",
        "STPBRR",
        "ATPPS",
        "ATCS",
        # identity
        "STI",
        "STIX",
        "STDI",
        "STDIX",
        "STMFR",
        "STSN",
        # voltage at pin 16 (read by the adapter itself; nothing is sent on the bus)
        "STVR",
        "STVR0",
        "STVR1",
        "STVR2",
        "STVR3",
        "STVRX",
        "ATRV",
        # CAN monitoring without ACKs
        "STCMM0",
        "STCMM2",
        # things that only reduce what the adapter sends
        "STPC",
        "ATPC",
        "ATSW00",
        "STPPMC",
        # clear filters
        "STFAC",
        "STFPC",
        # monitor
        "STM",
        "STMA",
    }
)

PATTERN_COMMANDS = (re.compile(r"STFPA[0-9A-F]{3,8},[0-9A-F]{3,8}"),)

# Accepted only through StnAdapter.start_can_monitor() and start_kline_monitor(), after their checks.
MONITOR_COMMANDS = frozenset({"STM", "STMA"})

CAN_MONITOR_PROTOCOLS = frozenset({"31", "33"})
KLINE_MONITOR_PROTOCOLS = frozenset({"21", "23"})

# Programmable parameter 21 holds the power-on default for CAN silent monitoring (FF = silent).
SILENT_MONITOR_PP = 0x21
_PP_ENTRY = re.compile(r"([0-9A-F]{2}):([0-9A-F]{2}) ([NF])")


def check_command(command: object) -> str:
    """Return the canonical form of an allowed command, or refuse it (audited)."""

    def deny(reason: str, detail: str) -> NoReturn:
        refuse(SafetyViolation(reason, detail), transport="stn", request=repr(command))

    if not isinstance(command, str) or not command:
        deny("adapter_command_empty", repr(command))
    bad = sorted({ch for ch in command if ch not in ALLOWED_INPUT})  # type: ignore[union-attr]
    if bad:
        deny("adapter_command_characters", repr("".join(bad)))
    canonical = command.replace(" ", "").upper()  # type: ignore[union-attr]
    if not canonical:
        deny("adapter_command_empty", repr(command))
    if set(canonical) <= _HEX_DIGITS:
        deny("adapter_hex_request", "a hex-only line is sent to the vehicle as a request")
    if canonical in EXACT_COMMANDS or canonical in RESET_COMMANDS:
        return canonical
    if any(pattern.fullmatch(canonical) for pattern in PATTERN_COMMANDS):
        return canonical
    deny("adapter_command_not_allowlisted", canonical)


def is_reset(canonical: str) -> bool:
    return canonical in RESET_COMMANDS


def is_monitor(canonical: str) -> bool:
    return canonical in MONITOR_COMMANDS


def parse_pp_summary(text: str) -> dict[int, tuple[int, bool]]:
    """ATPPS output ("21:FF F ...") as {parameter: (value, on)}."""
    return {int(num, 16): (int(value, 16), state == "N") for num, value, state in _PP_ENTRY.findall(text.upper())}


def silent_by_default(parameters: dict[int, tuple[int, bool]]) -> bool:
    """True if the adapter's power-on default is CAN silent monitoring (no ACKs).

    PP 21 off means the factory default applies, which is silent. PP 21 on is
    silent only with the value FF.
    """
    entry = parameters.get(SILENT_MONITOR_PP)
    if entry is None:
        return False
    value, on = entry
    return not on or value == 0xFF


freeze(__name__)
