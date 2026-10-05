"""FakeStnPort: stands in for the OBDLink MX+ serial port.

It follows the parsing rules in the STN manual. Case and spaces are ignored,
a backspace edits the line, a bare carriage return repeats the last command,
and any hex-only line is a request sent to the vehicle. It records a
violation for anything that would transmit on the vehicle bus, change the
adapter's saved settings, start monitoring without silent mode, or write
during the bootloader window after a reset. The rules are written here
independently of lasto.safety.stn_policy.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable

from lasto.sim.clock import FakeClock
from lasto.sim.violations import VIOLATIONS

REPLY_SECONDS = 0.02  # how long a read that finds its answer takes, on a clock
_HEX = re.compile(r"[0-9A-F]+")
# Commands that transmit on the vehicle bus, disable silent mode, or write the adapter's memory.
_DANGEROUS = (
    re.compile(r"STPX.*|STPO|ATRTR|ATSI|ATFI|STIFI.*|ATBI"),  # transmit or initialize the bus
    re.compile(r"ATSP.*|ATTP.*"),  # protocol search sends 01 00; ATSP also saves to memory
    re.compile(r"STPPMA.*|ATWM.*|ATSW(?!00$).*"),  # periodic messages and keep-alives
    re.compile(r"STCMM1|ATCSM0"),  # CAN ACKs on
    re.compile(r"ATPP(?!S$).*|ATSD.*|AT@3.*|STSAVCAL|STVCAL.*|ATCV.*|STWBR.*|STRSTNVM|STSL.*|STBT.*"),  # memory writes
    re.compile(r"ATSH.*|ATCP.*|ATTA.*|ATFCS.*|STCFCP.*|STFFCA.*|ATCEA.*"),  # header and flow-control shaping
    re.compile(r"STBR.*|STSBR.*|ATBRD.*|ATBRT.*|STSLEEP.*|ATLP|STGP.*|STBC.*"),  # link, sleep, GPIO, batch
)
_AUTOINIT_PROTOCOLS = frozenset({"22", "24", "25"})
_CAN_PROTOCOLS = frozenset({"31", "32", "33", "34", "35", "36"})
_KLINE_PROTOCOLS = frozenset({"21", "22", "23", "24", "25"})


class FakeStnPort:
    def __init__(
        self,
        *,
        pp21: tuple[int, bool] = (0xFF, False),
        voltage: str = "12.63",
        monitor_lines: Iterable[str] = (),
        monitor_ends: bool = False,
        banner: str = "ELM327 v1.4b",
        protocol_report: str | None = None,
        on_open: str = "",
        clock: FakeClock | None = None,
    ) -> None:
        """on_open is output already waiting when the link opens. If there is any, opening the link rebooted the
        adapter, and its bootloader window lasts until it has sent a prompt.

        With a clock, reads take time: one that finds what it waits for moves the clock on by REPLY_SECONDS, and
        one that doesn't waits out the port's timeout, as a real port does."""
        self.clock = clock
        self.timeout: float | None = 2.0
        self.write_timeout: float | None = 2.0
        self.closed = False
        self.pp = {0x00: (0xFF, False), 0x21: pp21}
        self.voltage = voltage
        self.monitor_lines: deque[str] = deque(monitor_lines)
        self.monitor_ends = monitor_ends
        self.banner = banner
        self.protocol_report = protocol_report
        self.commands: list[str] = []
        self.written = bytearray()
        self.monitoring = False
        self._line = bytearray()
        self._out = bytearray(on_open.encode("ascii"))
        self._bootloader_window = bool(on_open)
        self._reset_state()

    def _reset_state(self) -> None:
        self.echo = True
        self.protocol = "0"
        value, on = self.pp[0x21]
        self.silent = not on or value == 0xFF
        self.keepalive = True

    # ---- serial port interface ----

    def write(self, data: bytes) -> int:
        self.written += data
        for byte in data:
            self._feed(byte)
        return len(data)

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        if self.monitoring and expected not in self._out:
            self._produce_monitor_output()
        index = self._out.find(expected)
        if index == -1:
            data = bytes(self._out)
            self._out.clear()
        else:
            data = bytes(self._out[: index + len(expected)])
            del self._out[: index + len(expected)]
        if self._bootloader_window and data.endswith(b">"):
            self._bootloader_window = False
        if self.clock is not None:
            self.clock.advance(REPLY_SECONDS if data.endswith(expected) else (self.timeout or 0.0))
        return data

    def reset_input_buffer(self) -> None:
        self._out.clear()

    def close(self) -> None:
        self.closed = True

    # ---- adapter behavior ----

    def _produce_monitor_output(self) -> None:
        if self.monitor_lines:
            self._out += self.monitor_lines.popleft().encode("ascii") + b"\r"
        elif self.monitor_ends:
            self._out += b"BUFFER FULL\r\r>"
            self.monitoring = False

    def _feed(self, byte: int) -> None:
        if self._bootloader_window:
            VIOLATIONS.record("wrote to the adapter during the bootloader window after a reset")
        if self.monitoring:
            self.monitoring = False  # any character stops monitoring and is discarded
            self._out += b"STOPPED\r\r>"
            return
        if byte == 0x08:
            if self._line:
                self._line.pop()
            return
        if byte == 0x0D:
            line, self._line = bytes(self._line), bytearray()
            self._execute(line.decode("ascii", "replace").upper())
            return
        if byte == 0x20:
            return
        if not (chr(byte).isascii() and (chr(byte).isalnum() or chr(byte) == ",")):
            VIOLATIONS.record(f"unexpected byte 0x{byte:02X} sent to the adapter")
        self._line.append(byte)

    def _execute(self, text: str) -> None:
        self.commands.append(text)
        echo = text + "\r" if self.echo else ""
        if not text:
            VIOLATIONS.record("bare carriage return: the adapter repeats the last command")
            response: str | None = "?"
        elif _HEX.fullmatch(text):
            VIOLATIONS.record(f"hex line {text} would be sent to the vehicle as a request")
            response = "NO DATA"
        else:
            response = self._command(text)
        if response is None:  # monitoring started: output streams without a prompt
            self._out += echo.encode("ascii")
        else:
            self._out += (echo + response + "\r\r>").encode("ascii")

    def _command(self, text: str) -> str | None:
        for pattern in _DANGEROUS:
            if pattern.fullmatch(text):
                VIOLATIONS.record(f"dangerous adapter command {text}")
                return "OK"
        if text in ("ATZ", "ATWS"):
            self._reset_state()
            self._bootloader_window = True
            return f"\r{self.banner}"
        if text == "ATE0":
            self.echo = False
            return "OK"
        if text in ("ATL0", "ATS0", "ATH1", "ATD0", "ATD1", "ATCAF0", "ATCAF1", "ATM0", "STPC", "ATPC", "STPPMC"):
            return "OK"
        if text in ("STFAC", "STFPC") or text.startswith("STFPA"):
            return "OK"
        if text.startswith("STP") and text[3:].isdigit():
            protocol = text[3:]
            if protocol in _AUTOINIT_PROTOCOLS:
                VIOLATIONS.record(f"K-line protocol {protocol} initializes the bus")
            self.protocol = protocol
            return "OK"
        if text == "STPR":
            return self.protocol_report if self.protocol_report is not None else self.protocol
        if text in ("STCMM0", "STCMM2"):
            self.silent = True
            return "OK"
        if text == "ATSW00":
            self.keepalive = False
            return "OK"
        if text in ("STM", "STMA"):
            return self._start_monitor()
        if text in ("STVR0", "STVR1", "STVR2", "STVR3"):
            return f"{float(self.voltage):.{text[-1]}f}" if self.voltage.replace(".", "").isdigit() else self.voltage
        answers = {
            "STI": "STN2255 v5.10.3",
            "STIX": "STN2255 v5.10.3",
            "STDI": "OBDLink MX+ r3.2.1",
            "STDIX": "OBDLink MX+ r3.2.1",
            "STMFR": "OBD Solutions LLC",
            "STSN": "123456789012",
            "STVR": self.voltage,
            "STVRX": "0x7A3",
            "ATRV": self.voltage[:4] + "V",
            "ATPPS": self._pp_summary(),
            "ATCS": "T:00 R:00",
            "STPRS": "ISO 11898, 11-bit Tx, 500kbps, var DLC",
            "STPBRR": "500000",
        }
        if text in answers:
            return answers[text]
        VIOLATIONS.record(f"adapter command the simulator doesn't model: {text}")
        return "?"

    def _start_monitor(self) -> None:
        if self.protocol in _CAN_PROTOCOLS and not self.silent:
            VIOLATIONS.record("CAN monitoring started while the adapter ACKs frames")
        if self.protocol in _KLINE_PROTOCOLS and self.keepalive:
            VIOLATIONS.record("K-line monitoring started with keep-alives on")
        if self.protocol == "0":
            VIOLATIONS.record("monitoring with automatic protocol search sends requests")
        self.monitoring = True
        return None

    def _pp_summary(self) -> str:
        entries = [f"{num:02X}:{value:02X} {'N' if on else 'F'}" for num, (value, on) in sorted(self.pp.items())]
        return "\r".join("  ".join(entries[i : i + 4]) for i in range(0, len(entries), 4))
