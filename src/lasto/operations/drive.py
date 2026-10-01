"""lasto drive: passive capture in armed mode, from the simulator or the truck (docs/architecture.md §4).

One run is one process holding one channel open, listen-only, for as long as it runs:
- A session starts with the first frame. It ends after 60 s without one (the key turned off), and the
  next frame starts a new session (§0).
- The run stops on Ctrl+C, Ctrl+Break, the stop file in the data folder, the time limit, a storage
  failure, or when the safety core gives up on the channel. Every one takes the same path: read the
  channel once more, close the session, close the channel, index the audit log, and end the run.
- A storage failure never loses a drained frame silently, and never keeps the channel from closing
  (review finding L8). The recorder keeps what it was given and tries once more as the session
  closes; a session with anything still uncommitted is left open for the next capture's recovery.
  The failure and what it cost are recorded as a storage_error run event.
- Before it opens the channel, it takes the capture lock, recovers whatever an interrupted capture left
  open, and says how much disk it has: a warning below 2 GB free or over the data folder's 20 GB
  budget, never a refusal (lasto.storage.retention).
- Once a second it indexes the audit log, writes the live feed, and checks for the stop file.

It runs on the main thread, which holds the Ctrl+C and Ctrl+Break handlers, and, on the truck, the
keep-awake request.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from lasto import __version__
from lasto.capture.convert import to_records
from lasto.capture.recorder import Recorder
from lasto.capture.recovery import Recovery, recover
from lasto.operations.keep_awake import KeepAwake
from lasto.records import BusEvent, Frame, Record
from lasto.safety.audit import Auditor, JsonlAuditSink, hold_on_disk
from lasto.safety.clock import Clock, SystemClock
from lasto.safety.errors import InterfaceError, SafetyError
from lasto.safety.session import PassiveSession, open_passive_session
from lasto.services.safety_config import snapshot_text
from lasto.sim.vehicle import Sim, build_sim
from lasto.storage import capture_db, retention, workbench_db
from lasto.storage.audit_index import AuditIndex
from lasto.storage.capture_db import AuditLine
from lasto.storage.capture_lock import CaptureLock, CaptureRunning
from lasto.storage.ids import new_id
from lasto.storage.live_db import LiveFeed, LiveStatus
from lasto.storage.root import DataRoot

SILENCE = 60.0  # seconds without a frame that end a session (armed mode, §0)
POLL = 0.01  # seconds between reads of the channel
TICK = 1.0  # seconds between audit catch-ups, live feed writes, and stop-file checks
PROGRESS = 10.0  # seconds between progress lines
# Audit events about the channel, copied into the events table so a session's events tell its whole story.
MIRRORED = frozenset({
    "listen_only_rechecked", "passive_channel_distrusted", "passive_channel_reopened", "passive_reopen_failed",
    "session_ended", "rejected",
})  # fmt: skip
_SIGNALS = {signal.SIGINT: "ctrl_c", signal.SIGBREAK: "ctrl_break"}
# What stops a capture from starting, for the CLI to report: another capture, or the safety core refusing the channel.
CANNOT_START = (CaptureRunning, SafetyError, InterfaceError)


def hold_refusals(root: DataRoot) -> None:
    """Once per process, before anything else: refusals recorded while no audit log is attached also go to
    `audit/held.jsonl` in the data folder, fsynced as they're held, so a crash can't lose them."""
    root.ensure()
    hold_on_disk(root.audit_dir / "held.jsonl")


@dataclass(frozen=True)
class Source:
    """Where a drive's frames come from."""

    channel: str
    clock: Clock
    library: object | None  # the simulator's stand-in for PCANBasic.dll; None for the real one
    interface: str  # "simulator" or "pcan"

    @property
    def live(self) -> bool:
        return self.library is None


def simulated(sim: Sim | None = None, channel: str = "PCAN_USBBUS1") -> Source:
    """The simulator: its stand-in DLL, on its own clock, which runs as fast as the capture can go."""
    sim = build_sim() if sim is None else sim
    return Source(channel, sim.clock, sim.dll, "simulator")


def truck(channel: str) -> Source:
    """The PCAN-USB on the truck: the real driver, on the system clock."""
    return Source(channel, SystemClock(), None, "pcan")


@dataclass(frozen=True)
class DriveResult:
    run_id: str
    sessions: tuple[str, ...]
    frames: int
    end_reason: str
    recovery: Recovery


def drive(
    root: DataRoot,
    source: Source,
    *,
    seconds: float | None,
    report: Callable[[str], None] = print,
    keep_awake: KeepAwake | None = None,
    silence: float = SILENCE,
) -> DriveResult:
    """Capture until stopped. `seconds` is the time limit: simulated seconds in the simulator, where it's
    required, and an optional limit on the truck. On the truck, Windows is kept from sleeping (keep_awake
    stands in for that in tests)."""
    if not source.live and seconds is None:
        raise ValueError("a simulated drive needs a time limit in seconds")
    root.ensure()
    awake = keep_awake if keep_awake is not None else (KeepAwake() if source.live else None)
    with CaptureLock.take(root):
        conn = capture_db.open_capture(root)
        try:
            return _run(conn, root, source, seconds, report, awake, silence)
        finally:
            conn.close()


def _run(
    conn: sqlite3.Connection,
    root: DataRoot,
    source: Source,
    seconds: float | None,
    report: Callable[[str], None],
    awake: KeepAwake | None,
    silence: float,
) -> DriveResult:
    clock = source.clock
    recovery = recover(conn, root, now_utc=clock.utc_now().isoformat())
    _report_recovery(recovery, report)
    vehicle_id = _default_vehicle(root, clock)
    stop = _Stop(root.stop_file)
    started = clock.utc_now()
    audit_path = f"audit/{started:%Y%m%dT%H%M%SZ}-pid{os.getpid()}-{new_id()[:8]}.jsonl"
    open(root.absolute(audit_path), "x").close()  # a new file, never one another run wrote
    sink = JsonlAuditSink(root.absolute(audit_path))
    try:
        run_id = capture_db.create_run(
            conn,
            started_utc=started.isoformat(),
            pid=os.getpid(),
            mode="passive",
            interface=source.interface,
            channel=source.channel,
            lasto_version=__version__,
            safety_config=snapshot_text(),
            audit_path=audit_path,
        )
        capture = _Capture(conn, root, source, run_id, vehicle_id, started, report, silence, audit_path)
        with _stop_signals(stop), awake if awake is not None else contextlib.nullcontext():
            if awake is not None and not awake.held:
                report("Windows refused the keep-awake request; the laptop's power settings decide when it sleeps.")
            reason = capture.run(Auditor(sink, clock), stop, seconds)
    finally:
        sink.close()
    count = len(capture.sessions)
    report(f"Stopped ({reason}): {count} session{'' if count == 1 else 's'}, {capture.total:,} frames.")
    return DriveResult(run_id, tuple(capture.sessions), capture.total, reason, recovery)


def _report_recovery(recovery: Recovery, report: Callable[[str], None]) -> None:
    for session in recovery.sessions:
        damaged = f"; segments shorter than their index: {', '.join(session.damaged_segments)}" if session.damaged_segments else ""
        report(
            f"Recovered session {session.id}, left open by an interrupted capture: {session.seconds_indexed} seconds"
            f" indexed from its segments, {session.bytes_set_aside} bytes of torn tail set aside{damaged}."
        )
    for run_id in recovery.runs:
        report(f"Ended run {run_id}, left open by an interrupted capture.")


def _default_vehicle(root: DataRoot, clock: Clock) -> str:
    conn = workbench_db.open_workbench(root)
    try:
        return workbench_db.default_vehicle(conn, created_utc=clock.utc_now().isoformat())
    finally:
        conn.close()


class _Stop:
    """A request to stop at the next check: Ctrl+C, Ctrl+Break, or the stop file."""

    def __init__(self, stop_file: Path) -> None:
        self.reason: str | None = None
        self._file = stop_file
        self._file.unlink(missing_ok=True)  # a request left from before isn't for this capture

    def on_signal(self, signum: int, _frame: object) -> None:
        if self.reason is None:
            self.reason = _SIGNALS[signum]

    def check_file(self) -> None:
        if self.reason is None and self._file.exists():
            self.reason = "stop_file"
            self._file.unlink(missing_ok=True)


@contextlib.contextmanager
def _stop_signals(stop: _Stop) -> Iterator[None]:
    """Ctrl+C and Ctrl+Break ask for a clean stop instead of raising wherever the capture happens to be."""
    previous = {signum: signal.signal(signum, stop.on_signal) for signum in _SIGNALS}
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler if handler is not None else signal.SIG_DFL)


class _Capture:
    """One run's capture loop and what it keeps track of."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        root: DataRoot,
        source: Source,
        run_id: str,
        vehicle_id: str,
        started: datetime,
        report: Callable[[str], None],
        silence: float,
        audit_path: str,
    ) -> None:
        self.conn = conn
        self.root = root
        self.source = source
        self.clock = source.clock
        self.run_id = run_id
        self.vehicle_id = vehicle_id
        self.started = started
        self.report = report
        self.silence = silence
        self.audit = AuditIndex(conn, run_id, root.absolute(audit_path))
        self.live = LiveFeed(root.live_db, on_change=self._live_feed_changed)
        self.recorder: Recorder | None = None
        self.sessions: list[str] = []
        self.total = 0  # frames read from the channel
        self.session_errors = 0
        self.last_frame_at = 0.0
        self.tick_frames = 0
        self.tick_started = self.clock.monotonic()
        self.frames_per_second = 0
        self.bus: str | None = None
        # Storage failures (L8): the first one, and what it cost.
        self.storage_error: Exception | None = None
        self.left_open: list[str] = []
        self.frames_not_written = 0
        self.frames_not_indexed = 0
        self.events_not_written = 0

    def run(self, auditor: Auditor, stop: _Stop, seconds: float | None) -> str:
        """Open the channel, capture until something stops it, and close everything. Returns why it stopped."""
        reason = "error"
        self._check_disk()
        try:
            session = open_passive_session(
                self.source.channel, auditor=auditor, clock=self.clock, library=self.source.library
            )
        except BaseException as exc:
            reason = f"refused: {type(exc).__name__}"
            self._finish(reason)
            raise
        try:
            self._describe()
            reason = self._loop(session, stop, seconds)
            self.report(f"Stopping ({reason}).")
            if not session.ended:
                self._take(to_records(session.pump()), self.clock.monotonic())  # what arrived since the last read
        finally:
            try:
                if self.recorder is not None:
                    recorder, self.recorder = self.recorder, None
                    self._close(recorder, reason)
            finally:
                session.close()
                if self.storage_error is not None:
                    reason = "storage_error"
                self._finish(reason)
        return reason

    def _check_disk(self) -> None:
        """How much room the capture has, and a warning (never a refusal) if it's short or over budget."""
        try:
            check = retention.check_disk(self.root)
        except OSError as exc:
            self.report(f"Warning: couldn't check the disk space ({exc!r}); the capture goes on.")
            return
        self.report(check.summary())
        for warning in check.warnings:
            self.report(f"Warning: {warning}.")
            detail: dict[str, object] = {"warning": warning, "free_bytes": check.free_bytes, "used_bytes": check.used_bytes}
            self._store(
                lambda: capture_db.add_event(
                    self.conn,
                    run_id=self.run_id,
                    session_key=None,
                    host_utc=self.clock.utc_now().isoformat(),
                    hw_us=None,
                    kind="disk_warning",
                    detail=detail,
                )
            )

    def _finish(self, reason: str) -> None:
        try:
            self._store(lambda: self._mirror(self.audit.catch_up()))
            self.live.publish(self._status("stopped"))
        finally:
            self.live.close()
            if self.storage_error is not None:
                self._record_storage_error(self.storage_error)
            try:
                capture_db.end_run(self.conn, self.run_id, ended_utc=self.clock.utc_now().isoformat(), end_reason=reason)
            except Exception as exc:
                self.report(f"The run couldn't be ended in the capture database ({exc!r}); the next capture ends it.")

    def _store(self, write: Callable[[], object]) -> bool:
        """One of the capture's own database writes. A failure stops the run; it never raises."""
        try:
            write()
        except Exception as exc:
            self._storage_failed(exc)
            return False
        return True

    def _storage_failed(self, error: Exception) -> None:
        if self.storage_error is None:
            self.storage_error = error
            self.report(f"Storage failed ({error!r}). Stopping the capture.")

    def _record_storage_error(self, error: Exception) -> None:
        detail: dict[str, object] = {
            "error": f"{type(error).__name__}: {error}",
            "sessions_left_open": self.left_open,
            "frames_not_written": self.frames_not_written,
            "frames_not_indexed": self.frames_not_indexed,
            "events_not_written": self.events_not_written,
        }
        try:
            capture_db.add_event(
                self.conn,
                run_id=self.run_id,
                session_key=None,
                host_utc=self.clock.utc_now().isoformat(),
                hw_us=None,
                kind="storage_error",
                detail=detail,
            )
        except Exception as exc:
            self.report(f"The storage error couldn't be recorded in the capture database either ({exc!r}).")
        self.report(
            f"Storage error: {detail['error']}. Sessions left open for the next capture's recovery:"
            f" {len(self.left_open)}. Frames on disk that it will index: {self.frames_not_indexed:,}."
            f" Frames that couldn't be written: {self.frames_not_written:,}."
        )

    def _describe(self) -> None:
        """Record what the channel reported when it opened, from its session_opened audit record."""
        lines: list[AuditLine] = []
        self._store(lambda: lines.extend(self.audit.catch_up()))
        for line in lines:
            if line.event == "session_opened":
                opened = json.loads(line.record)
                hardware, api_version = str(opened.get("hardware", "")), str(opened.get("api_version", ""))
                self._store(
                    lambda: capture_db.describe_run(
                        self.conn,
                        self.run_id,
                        hardware=hardware,
                        api_version=api_version,
                        channel_version=str(opened.get("channel_version", "")),
                    )
                )
                self.report(
                    f"Run {self.run_id}: listen-only confirmed on {self.source.channel}"
                    f" ({hardware}, PCAN-Basic {api_version}). Waiting for traffic."
                )
        self._mirror(lines)

    def _loop(self, session: PassiveSession, stop: _Stop, seconds: float | None) -> str:
        start = self.clock.monotonic()
        deadline = None if seconds is None else start + seconds
        next_tick = start + TICK
        next_progress = start + PROGRESS
        while True:
            records = to_records(session.pump())
            now = self.clock.monotonic()
            self._take(records, now)
            if session.ended:
                self.report(f"The safety core closed the channel: {session.end_reason}")
                return "channel_lost"
            self._check_silence(now)
            if now >= next_tick:
                next_tick += TICK
                self._tick(now)
                stop.check_file()
            if now >= next_progress:
                next_progress += PROGRESS
                self._progress(now - start)
            if self.storage_error is not None:
                return "storage_error"
            if stop.reason is not None:
                return stop.reason
            if deadline is not None and now >= deadline:
                return "time_limit"
            self.clock.sleep(POLL)

    def _take(self, records: list[Record], now: float) -> None:
        for record in records:
            if isinstance(record, Frame):
                self.total += 1
                if self.recorder is None and (self.storage_error is not None or not self._start_session(record)):
                    self.frames_not_written += 1  # no session could take it
                    continue
                self.recorder.add(record)  # type: ignore[union-attr]
                self.tick_frames += 1
                self.session_errors += record.error
                self.last_frame_at = now
            else:
                self._bus_event(record)
        if self.recorder is not None:
            self.recorder.flush_due()
            if self.recorder.error is not None:
                self._storage_failed(self.recorder.error)

    def _start_session(self, first: Frame) -> bool:
        try:
            self.recorder = Recorder.start(
                self.conn, self.root, run_id=self.run_id, vehicle_id=self.vehicle_id, first=first, clock=self.clock
            )
        except Exception as exc:
            self._storage_failed(exc)
            return False
        self.sessions.append(self.recorder.session_id)
        self.session_errors = 0
        self.report(f"Session {self.recorder.session_id} started.")
        return True

    def _close(self, recorder: Recorder, reason: str) -> None:
        """Close a session. After a storage failure this is its one more try; it may leave the session open."""
        recorder.close(reason)
        self.frames_not_written += recorder.frames_not_written
        self.frames_not_indexed += recorder.frames_not_indexed
        self.events_not_written += recorder.events_not_written
        if recorder.error is not None:
            self._storage_failed(recorder.error)
        if recorder.left_open:
            self.left_open.append(recorder.session_id)
            self.report(f"Session {recorder.session_id} is left open; the next capture recovers it.")

    def _bus_event(self, event: BusEvent) -> None:
        self.bus = f"0x{event.status:05X}"
        if self.recorder is not None:
            self.recorder.add(event)
            return
        if self.storage_error is not None or not self._store(
            lambda: capture_db.add_event(
                self.conn,
                run_id=self.run_id,
                session_key=None,
                host_utc=self.clock.utc_now().isoformat(),
                hw_us=event.hw_us,
                kind=event.kind,
                detail={"status": self.bus},
            )
        ):
            self.events_not_written += 1

    def _check_silence(self, now: float) -> None:
        if self.recorder is None or now - self.last_frame_at < self.silence:
            return
        recorder, self.recorder = self.recorder, None
        self._close(recorder, "bus_silent")
        self.report(
            f"Session {recorder.session_id} ended after {self.silence:.0f} s without traffic: {recorder.frames:,} frames."
            " Waiting for traffic."
        )

    def _tick(self, now: float) -> None:
        self._store(lambda: self._mirror(self.audit.catch_up()))
        elapsed = now - self.tick_started
        self.frames_per_second = round(self.tick_frames / elapsed) if elapsed > 0 else 0
        self.tick_frames, self.tick_started = 0, now
        self.live.publish(self._status("capturing" if self.recorder is not None else "waiting"))

    def _progress(self, elapsed: float) -> None:
        if self.recorder is None:
            return
        self.report(f"{elapsed:7.0f} s  {self.recorder.frames:,} frames in this session, {self.frames_per_second:,} a second")

    def _mirror(self, lines: list[AuditLine]) -> None:
        for line in lines:
            if line.event not in MIRRORED:
                continue
            detail = {key: value for key, value in json.loads(line.record).items() if key not in ("event", "utc", "mono")}
            written = self._store(
                lambda: capture_db.add_event(
                    self.conn,
                    run_id=self.run_id,
                    session_key=None if self.recorder is None else self.recorder.session_key,
                    host_utc=line.utc,
                    hw_us=None,
                    kind=line.event,
                    detail=detail,
                )
            )
            if not written:
                return  # the audit table and the audit log itself still hold it

    def _status(self, state: str) -> LiveStatus:
        recorder = self.recorder if state != "stopped" else None
        return LiveStatus(
            pid=os.getpid(),
            run_id=self.run_id,
            session_id=None if recorder is None else recorder.session_id,
            state=state,
            interface=self.source.interface,
            channel=self.source.channel,
            started_utc=self.started.isoformat(),
            heartbeat_utc=self.clock.utc_now().isoformat(),
            frames=0 if recorder is None else recorder.frames,
            error_frames=0 if recorder is None else self.session_errors,
            frames_per_second=self.frames_per_second if recorder is not None else 0,
            bus=self.bus,
        )

    def _live_feed_changed(self, error: str | None) -> None:
        kind, detail = ("live_feed_failed", {"error": error}) if error is not None else ("live_feed_restored", {})
        self._store(
            lambda: capture_db.add_event(
                self.conn,
                run_id=self.run_id,
                session_key=None,
                host_utc=self.clock.utc_now().isoformat(),
                hw_us=None,
                kind=kind,
                detail=detail,
            )
        )
        self.report("The live feed can't be written; capture goes on." if error else "The live feed is written again.")
