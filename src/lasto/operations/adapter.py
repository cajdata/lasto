"""lasto adapter: the OBDLink MX+ on its own, for the Phase 3 bench test B3 (docs/architecture.md §12).

One run opens the adapter, resets it, reads what it is and the battery voltage, and, with a monitor, prints what it
hears until the time limit or Ctrl+C. Then it stops the monitor and closes the adapter. Everything it sends goes
through the safety core's STN link (lasto.safety.stn_port): the command allowlist, the audit log, and the
bootloader-window rules (§3.7). Nothing it sends reaches the vehicle bus: CAN monitoring is silent (no ACKs), and
K-line monitoring starts no bus initialization and sends no keep-alives.

- Opening sends nothing until the adapter settles. How long that took is reported, for B3 to measure: the settle
  time if the adapter stayed quiet, less if it sent its prompt.
- The audit log is a new file in the data folder's audit folder, as a drive's is.
- Ctrl+C or Ctrl+Break stops the monitor at the next line it reads, then the run closes the adapter.
- When the safety core refuses what the adapter sent, or the link drops (Bluetooth off), the run ends and says why.
  The adapter is closed, and nothing retries it.
- The simulator's MX+ runs on its own clock, so a simulated monitor needs a time limit.
"""

from __future__ import annotations

import contextlib
import os
import signal
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from lasto.safety.audit import Auditor, JsonlAuditSink
from lasto.safety.clock import Clock, SystemClock
from lasto.safety.errors import AdapterError
from lasto.safety.serial_guard import allow_writes_in
from lasto.safety.stn_port import StnAdapter, open_adapter
from lasto.sim.clock import FakeClock
from lasto.sim.fake_stn import FakeStnPort
from lasto.storage.ids import new_id
from lasto.storage.root import DataRoot

MONITORS = ("can", "kline")
_SIGNALS = {signal.SIGINT: "ctrl_c", signal.SIGBREAK: "ctrl_break"}

# What the simulated MX+ hears while it monitors, with headers on and spaces off as reset() sets them: a few of the
# truck's broadcast IDs (§4) on CAN, and a K-line answer.
SIMULATED_CAN = ("0250F5A000000000000", "2C40011223344556677", "4C10000000000000000", "0230000000000000000")
SIMULATED_KLINE = ("8110F12101A4",)


@dataclass(frozen=True)
class AdapterSource:
    """Which MX+ a check opens."""

    port: str | None  # COMn on the bench; None for the simulator's
    clock: Clock
    simulated_port: FakeStnPort | None  # the simulator's MX+; None on the bench

    @property
    def live(self) -> bool:
        return self.simulated_port is None


def simulated(port: FakeStnPort | None = None, *, monitor: str | None = None) -> AdapterSource:
    """The simulator's MX+, on its own clock: a read that hears nothing moves the clock on by the port's timeout."""
    if port is None:
        lines = SIMULATED_KLINE if monitor == "kline" else SIMULATED_CAN
        port = FakeStnPort(clock=FakeClock(), monitor_lines=lines * 25)
    if port.clock is None:
        raise ValueError("the simulated MX+ needs a clock, which its reads move on")
    return AdapterSource(None, port.clock, port)


def bench(port: str) -> AdapterSource:
    """The MX+ on a COM port: the real link, on the system clock."""
    return AdapterSource(port, SystemClock(), None)


@dataclass(frozen=True)
class AdapterResult:
    end_reason: str  # done, time_limit, ctrl_c, ctrl_break, monitor_ended, or adapter_error
    settled_after: float | None  # seconds the open took; None if the adapter didn't open
    banner: str
    identity: dict[str, str]
    voltage: float | None
    lines: int  # monitor lines heard


def check(
    root: DataRoot,
    source: AdapterSource,
    *,
    monitor: str | None,
    seconds: float | None,
    report: Callable[[str], None] = print,
) -> AdapterResult:
    """Open the MX+, reset and identify it, and with `monitor` ("can" or "kline") listen until `seconds` pass
    (simulated seconds in the simulator, where a monitor needs them) or Ctrl+C."""
    if monitor is not None and monitor not in MONITORS:
        raise ValueError(f"monitor is one of {', '.join(MONITORS)}, not {monitor!r}")
    if monitor is not None and not source.live and seconds is None:
        raise ValueError("a simulated monitor needs a time limit in seconds")
    root.ensure()
    allow_writes_in(root.path)  # guard v2: the data folder is where this process writes (the same folder again is fine)
    clock = source.clock
    audit_path = root.audit_dir / f"{clock.utc_now():%Y%m%dT%H%M%SZ}-pid{os.getpid()}-adapter-{new_id()[:8]}.jsonl"
    open(audit_path, "x").close()  # a new file, never one another run wrote
    sink = JsonlAuditSink(audit_path)
    try:
        result = _check(Auditor(sink, clock), source, monitor, seconds, report)
    finally:
        sink.close()
    report(f"Audit log: {audit_path}")
    return result


def _check(
    auditor: Auditor, source: AdapterSource, monitor: str | None, seconds: float | None, report: Callable[[str], None]
) -> AdapterResult:
    clock = source.clock
    opened = clock.monotonic()
    try:
        if source.simulated_port is None:
            stn = open_adapter(source.port, auditor=auditor)  # type: ignore[arg-type]  (a bench source has its port)
        else:
            stn = StnAdapter(source.simulated_port, auditor)
    except AdapterError as error:
        report(f"The adapter didn't open: {error}")
        report("Stopped (adapter_error).")
        return AdapterResult("adapter_error", None, "", {}, None, 0)
    settled = clock.monotonic() - opened
    report(f"Opened. It settled after {settled:.2f} s (the whole settle time if it stayed quiet; less if it sent its prompt).")
    reason, banner, identity, voltage, lines = "adapter_error", "", {}, None, 0
    try:
        with _stop_signals() as stop:
            banner = stn.reset()
            report(f"Reset: {banner}")
            identity = stn.identify()
            report(f"It's an {identity['device']}, firmware {identity['firmware']}, serial {identity['serial']}.")
            voltage = stn.read_voltage()
            report(f"Battery at the adapter: {voltage:.2f} V")
            if monitor is None:
                reason = stop.reason or "done"
            else:
                reason, lines = _monitor(stn, monitor, seconds, clock, stop, report)
    except AdapterError as error:
        report(f"The safety core stopped the check: {error}")
    finally:
        try:
            stn.close()
        except AdapterError as error:
            report(f"Closing the adapter: {error}")
            reason = "adapter_error"
    report(f"Stopped ({reason}).")
    return AdapterResult(reason, settled, banner, identity, voltage, lines)


def _monitor(
    stn: StnAdapter, monitor: str, seconds: float | None, clock: Clock, stop: _Stop, report: Callable[[str], None]
) -> tuple[str, int]:
    limit = "until Ctrl+C" if seconds is None else f"for {seconds:g} s, or until Ctrl+C"
    if monitor == "can":
        stn.start_can_monitor()
        report(f"Monitoring CAN, silently: the adapter sends no ACKs. Listening {limit}.")
    else:
        stn.start_kline_monitor()
        report(f"Monitoring the K-line: no bus initialization, keep-alives off. Listening {limit}.")
    started = clock.monotonic()
    lines = 0
    while True:
        elapsed = clock.monotonic() - started
        if stop.reason is not None:
            reason = stop.reason
            break
        if seconds is not None and elapsed >= seconds:
            reason = "time_limit"
            break
        line = stn.read_monitor_line()
        if line is None and not stn.monitoring:
            reason = "monitor_ended"
            break
        if line:
            lines += 1
            report(f"  {clock.monotonic() - started:9.3f}  {line}")
    if stn.monitoring:
        after = stn.stop_monitor()
        heard = f", after {', '.join(after)}" if after else ""
        report(f"Stopped the monitor with a backspace: the prompt came back{heard}.")
    report(f"Heard {lines} line{'' if lines == 1 else 's'}.")
    return reason, lines


class _Stop:
    """A request to stop at the next monitor line: Ctrl+C or Ctrl+Break."""

    def __init__(self) -> None:
        self.reason: str | None = None

    def on_signal(self, signum: int, _frame: object) -> None:
        if self.reason is None:
            self.reason = _SIGNALS[signum]


@contextlib.contextmanager
def _stop_signals() -> Iterator[_Stop]:
    """Ctrl+C and Ctrl+Break ask for a clean stop, instead of raising wherever the check happens to be."""
    stop = _Stop()
    previous = {signum: signal.signal(signum, stop.on_signal) for signum in _SIGNALS}
    try:
        yield stop
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler if handler is not None else signal.SIG_DFL)
