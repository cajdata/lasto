"""OBDLink MX+ (STN) adapter link (rule 4).

The only code in lasto that opens a serial port or writes to one. Every line
is checked against the exact-match allowlist in stn_policy and recorded in
the audit log before it is written. Nothing this module sends reaches the
vehicle bus: request lines are denied, and CAN monitoring starts only after
the silent-mode checks in docs/architecture.md §3.7 pass. K-line monitoring
uses a preset with no bus initialization and keep-alives switched off.
Reset and monitor commands are accepted only inside the routines that
enforce those rules. Every refusal is audited (rule 11).
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable
from types import ModuleType
from typing import NoReturn, Protocol

from lasto.safety import stn_policy
from lasto.safety._frozen import SealedProtocolType, SealedType, freeze
from lasto.safety.audit import REFUSALS, Auditor, refuse
from lasto.safety.errors import AdapterError, SafetyViolation
from lasto.safety.serial_guard import PYSERIAL_IMPORT

PROMPT = b">"
# Backspace stops a monitor; on an idle adapter it edits an empty line, so nothing is left behind.
STOP_MONITOR = b"\x08"
COMMAND_TIMEOUT = 2.0
RESET_TIMEOUT = 5.0
# How long an adapter that sends nothing when the link opens must stay quiet before it's taken as booted and idle.
# Provisional: the bench test (B3) measures how long the MX+ takes to boot.
SETTLE_TIMEOUT = 3.0
CONFIGURE_COMMANDS = ("ATE0", "ATL0", "ATS0", "ATH1", "ATM0")

_PORT_NAME = re.compile(r"COM[1-9][0-9]{0,2}")
_VOLTAGE = re.compile(r"\d{1,2}\.\d{1,3}")
# What the adapter prints as it boots (ATZ, power, or a Bluetooth reconnect or wake). Confirm on the bench (B3).
_BANNER = re.compile(r"ELM327 v\d\S*")


class SerialPort(Protocol, metaclass=SealedProtocolType):
    timeout: float | None

    def write(self, data: bytes) -> int | None:
        """Write bytes to the adapter."""

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        """Read until `expected` arrives or the timeout passes."""

    def close(self) -> None:
        """Close the port."""


def _pyserial() -> ModuleType:
    """pyserial, imported where guard v2 lets it look up its kernel32 functions (CreateFileW among them)."""
    with PYSERIAL_IMPORT:
        import serial  # only imported when real hardware is opened
    return serial


def _open_serial(port_name: str, *, factory: Callable[..., SerialPort] | None) -> SerialPort:
    if not isinstance(port_name, str) or _PORT_NAME.fullmatch(port_name) is None:
        refuse(ValueError(f"not a COM port name: {port_name!r}"), transport="stn", reason="bad_port_name")
    if factory is None:
        factory = _pyserial().Serial
    try:
        return factory(port=port_name, baudrate=115200, timeout=COMMAND_TIMEOUT, write_timeout=COMMAND_TIMEOUT)
    except OSError as error:  # pyserial's SerialException: no such port, or another program holds it
        _reject("adapter_open_failed", f"could not open {port_name}: {error}", f"open {port_name}")


def open_adapter(port_name: str, *, auditor: Auditor, factory: Callable[..., SerialPort] | None = None) -> StnAdapter:
    """Open the OBDLink on a COM port (or a stand-in `factory`, such as the simulator's).

    The port itself is never handed out: it lives inside the adapter, so every
    line reaching it goes through the allowlist and the audit log (finding N1).
    The audit log is attached first, so a refused port name or a failed open is
    written to it too (finding L4).
    """
    REFUSALS.attach(auditor)
    try:
        return StnAdapter(_open_serial(port_name, factory=factory), auditor)
    finally:
        REFUSALS.detach(auditor)  # the adapter holds its own attachment while it's open


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\n", "\r").split("\r") if line.strip()]


def _has_banner(lines: list[str]) -> bool:
    return any(_BANNER.fullmatch(line) for line in lines)


def _reject(reason: str, detail: str, request: str = "") -> NoReturn:
    """Refuse (audited) an answer or state the adapter link can't accept."""
    refuse(AdapterError(detail), transport="stn", request=request, reason=reason)


class StnAdapter(metaclass=SealedType):
    """The OBDLink, from the moment its link opens (finding E, the bootloader window):

    - Opening sends nothing. The adapter is read until a prompt arrives (after its banner, if opening the link
      rebooted it), or until SETTLE_TIMEOUT passes with nothing at all. Output with no prompt refuses the open.
    - ATZ goes only through reset(), which sends nothing more until the prompt is back.
    - After a prompt that never came, or a banner outside reset() (the adapter rebooted), its state is unknown:
      it refuses every command until it's opened again, so ATZ is never sent blindly.
    - A failed read or write (a Bluetooth drop: pyserial's SerialException is an OSError) closes it, audited,
      and nothing retries it.
    """

    __slots__ = ("_auditor", "_closed", "_configured", "_monitoring", "_port", "_rx", "_unknown")

    def __init__(self, port: SerialPort, auditor: Auditor) -> None:
        self._port = port
        self._auditor = auditor
        self._configured = False
        self._monitoring = False
        self._closed = False
        self._unknown = ""  # why the adapter's state is unknown, once it is
        self._rx = bytearray()
        REFUSALS.attach(auditor)
        try:
            self._settle()
        except BaseException:
            with contextlib.suppress(OSError):
                self._port.close()
            REFUSALS.detach(auditor)
            raise

    @property
    def monitoring(self) -> bool:
        return self._monitoring

    def _settle(self) -> None:
        """Wait out the bootloader window without sending anything."""
        self._port.timeout = SETTLE_TIMEOUT
        try:
            data = self._receive(PROMPT, "open the adapter")
        finally:
            self._port.timeout = COMMAND_TIMEOUT
        if data and not data.endswith(PROMPT):
            _reject(
                "adapter_not_settled",
                f"the adapter sent {data!r} with no prompt when its link opened; power-cycle it and open it again",
                "open the adapter",
            )

    # ---- the adapter's state ----

    def _require_usable(self, request: str) -> None:
        if self._closed:
            _reject("adapter_closed", "the adapter is closed; open it again", request)
        if self._unknown:
            _reject("adapter_state_unknown", f"{self._unknown}, so nothing is sent until it's opened again", request)

    def _no_prompt(self, data: bytes, request: str) -> NoReturn:
        self._unknown = "a prompt never came back"
        _reject("adapter_no_prompt", f"no prompt from the adapter (got {data!r}); open it again before anything else", request)

    def _rebooted(self, request: str) -> NoReturn:
        self._unknown = "the adapter rebooted mid-session"
        self._monitoring = False
        _reject("adapter_rebooted", f"the adapter's banner arrived during {request}: it rebooted", request)

    def _lose_link(self, error: OSError, request: str) -> NoReturn:
        self._closed = True
        self._monitoring = False
        with contextlib.suppress(OSError):
            self._port.close()
        _reject("adapter_link_lost", f"the link to the adapter failed ({error}); it's closed, and nothing retries it", request)

    # ---- the only writes to the serial port, and its reads ----

    def _send(self, data: bytes, request: str) -> None:
        try:
            self._port.write(data)
        except OSError as error:
            self._lose_link(error, request)

    def _receive(self, expected: bytes, request: str) -> bytes:
        try:
            return self._port.read_until(expected)
        except OSError as error:
            self._lose_link(error, request)

    def _write_command(self, command: str, *, reset: bool = False, monitor: bool = False) -> str:
        canonical = stn_policy.check_command(command)
        if stn_policy.is_reset(canonical) and not reset:
            refuse(SafetyViolation("adapter_reset_outside_reset_routine", canonical), transport="stn", request=canonical)
        if stn_policy.is_monitor(canonical) and not monitor:
            refuse(SafetyViolation("adapter_monitor_outside_monitor_routine", canonical), transport="stn", request=canonical)
        self._require_usable(canonical)
        if self._monitoring:
            refuse(
                SafetyViolation("adapter_is_monitoring", "stop monitoring before sending commands"),
                transport="stn",
                request=canonical,
            )
        self._auditor.adapter_command(command=canonical)
        self._send(canonical.encode("ascii") + b"\r", canonical)
        return canonical

    def _write_stop(self) -> None:
        self._require_usable("stop monitoring")
        self._auditor.adapter_command(command="<backspace: stop monitoring>")
        self._send(STOP_MONITOR, "stop monitoring")

    # ---- reading ----

    def _read_prompt(self, request: str) -> str:
        data = self._receive(PROMPT, request)
        if not data.endswith(PROMPT):
            self._no_prompt(data, request)
        return data[:-1].replace(b"\x00", b"").decode("ascii", "replace")

    def _response(self, canonical: str, *, booting: bool = False) -> list[str]:
        lines = _lines(self._read_prompt(canonical))
        # Until ATE0 takes effect, the adapter echoes the command first.
        if lines and lines[0].replace(" ", "").upper() == canonical:
            lines = lines[1:]
        if not booting and _has_banner(lines):
            self._rebooted(canonical)
        return lines

    def _command(self, command: str) -> str:
        return "\n".join(self._response(self._write_command(command)))

    def _expect(self, command: str, expected: str) -> None:
        response = self._command(command)
        if response != expected:
            _reject("adapter_unexpected_answer", f"{command} answered {response!r}, expected {expected!r}", command)

    def _require_configured(self, operation: str) -> None:
        self._require_usable(operation)
        if not self._configured:
            _reject("adapter_not_reset", "reset the adapter first", operation)

    # ---- operations ----

    def reset(self) -> str:
        """ATZ, wait for the banner and prompt (the bootloader window), then set output format."""
        self._configured = False
        canonical = self._write_command("ATZ", reset=True)
        self._port.timeout = RESET_TIMEOUT
        try:
            banner = " ".join(self._response(canonical, booting=True))
        finally:
            self._port.timeout = COMMAND_TIMEOUT
        for command in CONFIGURE_COMMANDS:
            self._expect(command, "OK")
        self._configured = True
        return banner

    def identify(self) -> dict[str, str]:
        self._require_configured("identify")
        return {"firmware": self._command("STI"), "device": self._command("STDI"), "serial": self._command("STSN")}

    def read_voltage(self) -> float:
        """Battery voltage at OBD pin 16, measured by the adapter."""
        self._require_configured("read the voltage")
        text = self._command("STVR")
        if _VOLTAGE.fullmatch(text) is None:
            _reject("adapter_bad_voltage", f"unexpected voltage reading {text!r}", "STVR")
        return float(text)

    def programmable_parameters(self) -> dict[int, tuple[int, bool]]:
        self._require_configured("read the programmable parameters")
        return stn_policy.parse_pp_summary(self._command("ATPPS"))

    def start_can_monitor(self, protocol: str = "31") -> None:
        """Silent CAN monitoring. Runs every check in docs/architecture.md §3.7 first; each failure is audited."""
        what = f"start CAN monitor on protocol {protocol!r}"
        if protocol not in stn_policy.CAN_MONITOR_PROTOCOLS:
            refuse(ValueError(f"CAN monitoring uses protocol 31 or 33, not {protocol!r}"), transport="stn", request=what, reason="bad_monitor_protocol")
        if not stn_policy.silent_by_default(self.programmable_parameters()):
            _reject("adapter_acks_by_default", "PP 21 makes the adapter ACK CAN frames by default", what)
        self._expect(f"STP{protocol}", "OK")
        self._expect("STPR", protocol)
        self._expect("STCMM0", "OK")
        self._write_command("STMA", monitor=True)
        self._monitoring = True

    def start_kline_monitor(self, protocol: str = "23") -> None:
        """Passive K-line monitoring: a preset with no bus initialization, keep-alives off. Each failure is audited."""
        what = f"start K-line monitor on protocol {protocol!r}"
        self._require_configured(what)
        if protocol not in stn_policy.KLINE_MONITOR_PROTOCOLS:
            refuse(ValueError(f"K-line monitoring uses protocol 21 or 23, not {protocol!r}"), transport="stn", request=what, reason="bad_monitor_protocol")
        self._expect("ATSW00", "OK")
        self._expect(f"STP{protocol}", "OK")
        self._expect("STPR", protocol)
        self._write_command("STMA", monitor=True)
        self._monitoring = True

    def read_monitor_line(self) -> str | None:
        """The next complete line of monitor output, or None if none arrived in time.

        If the monitor ends on its own (for example BUFFER FULL), the prompt
        comes back, monitoring is marked stopped, and None is returned.
        """
        self._require_usable("read a monitor line")
        if not self._monitoring:
            _reject("adapter_not_monitoring", "the adapter isn't monitoring", "read a monitor line")
        self._rx.extend(self._receive(b"\r", "read a monitor line"))
        if self._rx.endswith(b"\r"):
            line = bytes(self._rx[:-1]).replace(b"\x00", b"").decode("ascii", "replace").strip()
            self._rx.clear()
            if _BANNER.fullmatch(line):
                self._rebooted("read a monitor line")
            return line
        if self._rx.endswith(PROMPT):
            self._rx.clear()
            self._monitoring = False
        return None

    def stop_monitor(self) -> list[str]:
        """Stop monitoring and return any lines that arrived before it stopped."""
        if not self._monitoring:
            return []
        self._write_stop()
        text = (bytes(self._rx) + self._read_prompt("stop monitoring").encode("ascii", "replace")).decode("ascii", "replace")
        self._rx.clear()
        self._monitoring = False
        lines = _lines(text)
        if _has_banner(lines):
            self._rebooted("stop monitoring")
        return lines

    def close(self) -> None:
        self._closed = True
        self._port.close()
        REFUSALS.detach(self._auditor)


freeze(__name__)
