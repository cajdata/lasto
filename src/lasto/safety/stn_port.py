"""OBDLink MX+ (STN) adapter link (rule 4).

The only code in lasto that opens a serial port or writes to one. Every line
is checked against the exact-match allowlist in stn_policy and recorded in
the audit log before it is written. Nothing this module sends reaches the
vehicle bus: request lines are denied, and CAN monitoring starts only after
the silent-mode checks in docs/architecture.md §3.7 pass. K-line monitoring
uses a preset with no bus initialization and keep-alives switched off.
Reset and monitor commands are accepted only inside the routines that
enforce those rules.
"""

from __future__ import annotations

import re
from typing import Protocol

from lasto.safety import stn_policy
from lasto.safety.audit import Auditor
from lasto.safety.errors import AdapterError, SafetyViolation

PROMPT = b">"
# Backspace stops a monitor; on an idle adapter it edits an empty line, so nothing is left behind.
STOP_MONITOR = b"\x08"
COMMAND_TIMEOUT = 2.0
RESET_TIMEOUT = 5.0
CONFIGURE_COMMANDS = ("ATE0", "ATL0", "ATS0", "ATH1", "ATM0")

_PORT_NAME = re.compile(r"COM[1-9][0-9]{0,2}")
_VOLTAGE = re.compile(r"\d{1,2}\.\d{1,3}")


class SerialPort(Protocol):
    timeout: float | None

    def write(self, data: bytes) -> int | None:
        """Write bytes to the adapter."""

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        """Read until `expected` arrives or the timeout passes."""

    def close(self) -> None:
        """Close the port."""


def open_serial(port_name: str, *, factory: object = None) -> SerialPort:
    """Open a COM port for the adapter. Only live hardware sessions call this without a factory."""
    if not isinstance(port_name, str) or _PORT_NAME.fullmatch(port_name) is None:
        raise ValueError(f"not a COM port name: {port_name!r}")
    if factory is None:
        import serial  # pyserial; only imported when real hardware is opened

        factory = serial.Serial
    return factory(port=port_name, baudrate=115200, timeout=COMMAND_TIMEOUT, write_timeout=COMMAND_TIMEOUT)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\n", "\r").split("\r") if line.strip()]


class StnAdapter:
    def __init__(self, port: SerialPort, auditor: Auditor) -> None:
        self._port = port
        self._auditor = auditor
        self._configured = False
        self._monitoring = False
        self._rx = bytearray()

    @property
    def monitoring(self) -> bool:
        return self._monitoring

    # ---- the only writes to the serial port ----

    def _write_command(self, command: str, *, reset: bool = False, monitor: bool = False) -> str:
        try:
            canonical = stn_policy.check_command(command)
            if stn_policy.is_reset(canonical) and not reset:
                raise SafetyViolation("adapter_reset_outside_reset_routine", canonical)
            if stn_policy.is_monitor(canonical) and not monitor:
                raise SafetyViolation("adapter_monitor_outside_monitor_routine", canonical)
            if self._monitoring:
                raise SafetyViolation("adapter_is_monitoring", "stop monitoring before sending commands")
        except SafetyViolation as exc:
            self._auditor.rejected(transport="stn", reason=exc.reason, detail=str(exc), request=repr(command))
            raise
        self._auditor.adapter_command(command=canonical)
        self._port.write(canonical.encode("ascii") + b"\r")
        return canonical

    def _write_stop(self) -> None:
        self._auditor.adapter_command(command="<backspace: stop monitoring>")
        self._port.write(STOP_MONITOR)

    # ---- reading ----

    def _read_prompt(self) -> str:
        data = self._port.read_until(PROMPT)
        if not data.endswith(PROMPT):
            raise AdapterError(f"no prompt from the adapter (got {data!r})")
        return data[:-1].replace(b"\x00", b"").decode("ascii", "replace")

    def _response(self, canonical: str) -> list[str]:
        lines = _lines(self._read_prompt())
        # Until ATE0 takes effect, the adapter echoes the command first.
        if lines and lines[0].replace(" ", "").upper() == canonical:
            lines = lines[1:]
        return lines

    def _command(self, command: str) -> str:
        return "\n".join(self._response(self._write_command(command)))

    def _expect(self, command: str, expected: str) -> None:
        response = self._command(command)
        if response != expected:
            raise AdapterError(f"{command} answered {response!r}, expected {expected!r}")

    def _require_configured(self) -> None:
        if not self._configured:
            raise AdapterError("reset the adapter first")

    # ---- operations ----

    def reset(self) -> str:
        """ATZ, wait for the banner and prompt (the bootloader window), then set output format."""
        self._configured = False
        canonical = self._write_command("ATZ", reset=True)
        self._port.timeout = RESET_TIMEOUT
        try:
            banner = " ".join(self._response(canonical))
        finally:
            self._port.timeout = COMMAND_TIMEOUT
        for command in CONFIGURE_COMMANDS:
            self._expect(command, "OK")
        self._configured = True
        return banner

    def identify(self) -> dict[str, str]:
        self._require_configured()
        return {"firmware": self._command("STI"), "device": self._command("STDI"), "serial": self._command("STSN")}

    def read_voltage(self) -> float:
        """Battery voltage at OBD pin 16, measured by the adapter."""
        self._require_configured()
        text = self._command("STVR")
        if _VOLTAGE.fullmatch(text) is None:
            raise AdapterError(f"unexpected voltage reading {text!r}")
        return float(text)

    def programmable_parameters(self) -> dict[int, tuple[int, bool]]:
        self._require_configured()
        return stn_policy.parse_pp_summary(self._command("ATPPS"))

    def start_can_monitor(self, protocol: str = "31") -> None:
        """Silent CAN monitoring. Runs every check in docs/architecture.md §3.7 first."""
        if protocol not in stn_policy.CAN_MONITOR_PROTOCOLS:
            raise ValueError(f"CAN monitoring uses protocol 31 or 33, not {protocol!r}")
        if not stn_policy.silent_by_default(self.programmable_parameters()):
            raise AdapterError("PP 21 makes the adapter ACK CAN frames by default; refusing to monitor")
        self._expect(f"STP{protocol}", "OK")
        self._expect("STPR", protocol)
        self._expect("STCMM0", "OK")
        self._write_command("STMA", monitor=True)
        self._monitoring = True

    def start_kline_monitor(self, protocol: str = "23") -> None:
        """Passive K-line monitoring: a preset with no bus initialization, keep-alives off."""
        self._require_configured()
        if protocol not in stn_policy.KLINE_MONITOR_PROTOCOLS:
            raise ValueError(f"K-line monitoring uses protocol 21 or 23, not {protocol!r}")
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
        if not self._monitoring:
            raise AdapterError("the adapter isn't monitoring")
        self._rx.extend(self._port.read_until(b"\r"))
        if self._rx.endswith(b"\r"):
            line = bytes(self._rx[:-1]).replace(b"\x00", b"").decode("ascii", "replace").strip()
            self._rx.clear()
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
        text = (bytes(self._rx) + self._read_prompt().encode("ascii", "replace")).decode("ascii", "replace")
        self._rx.clear()
        self._monitoring = False
        return _lines(text)

    def close(self) -> None:
        self._port.close()
