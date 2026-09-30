"""Records one session: its raw frames to segment files, and each second to the capture database.

Raw frames reach disk first (docs/architecture.md §1, rule 2 of the system's shape):
- A second is written to its segment and fsynced before its database transaction commits, so the
  database never points at data that isn't on disk.
- A crash costs at most the second being written: a second written but not committed is found and
  indexed by the next capture's recovery (lasto.capture.recovery).

Storage failures (review finding L8): nothing the recorder was given is dropped.
- A second stays in memory until it commits. A failed segment write is cut back off the file, and a
  failed commit leaves the second on disk, so either can be tried again.
- After a failure the recorder stops writing until close(), and never raises from add() or flush_due():
  the capture reads `error` and stops.
- close() tries once more, in order. Each second goes to its segment, and commits go on while they
  work. Once a commit fails, later seconds go to the segment only, so the database still indexes the
  start of the file and nothing past it.
- If anything is left uncommitted, the session stays open, and the next capture's recovery indexes
  what reached disk. What reached neither is counted in frames_not_written.

Time:
- A second is a whole second of hardware time from the session's first frame.
- Candump timestamps come from the session's time base: that first frame's hardware timestamp, and
  the UTC it arrived at.
- A clock anchor (hardware timestamp, host UTC, host monotonic) is recorded every minute, for drift.

A new segment file starts every hour of the session.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from lasto.capture.rollups import LastSeen, summarize
from lasto.records import BusEvent, Frame
from lasto.safety.clock import Clock
from lasto.storage import capture_db
from lasto.storage.root import DataRoot
from lasto.storage.segments import SegmentFile, TimeBase

FLUSH_AFTER = 1.5  # seconds of host time before a quiet second is written anyway
SEGMENT_SECONDS = 3600  # a new segment file every hour
ANCHOR_EVERY = 60.0  # seconds of host time between clock anchors
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def utc_us(moment: datetime) -> int:
    """A UTC datetime as whole microseconds since the epoch, exactly."""
    return (moment - _EPOCH) // timedelta(microseconds=1)


def utc_text(microseconds: int) -> str:
    return (_EPOCH + timedelta(microseconds=microseconds)).isoformat()


@dataclass
class _Second:
    second: int
    started: float  # host monotonic time its first frame arrived
    frames: list[Frame]
    on_disk: tuple[int, int, int] | None = field(default=None)  # (segment, byte offset, byte length) once written


class Recorder:
    """One open session. Frames go in with add(); close() writes what's left and closes the session."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        root: DataRoot,
        session: capture_db.OpenedSession,
        base: TimeBase,
        folder: str,
        clock: Clock,
        *,
        run_id: str,
        segment_seconds: int,
        anchor_every: float,
    ) -> None:
        self._conn = conn
        self._root = root
        self._session = session
        self._base = base
        self._folder = folder
        self._clock = clock
        self._run_id = run_id
        self._segment_seconds = segment_seconds
        self._anchor_every = anchor_every
        self._last_anchor = clock.monotonic()
        self._open: _Second | None = None  # the second being collected
        self._ready: list[_Second] = []  # seconds sealed but not committed, oldest first
        self._events: list[tuple[str, BusEvent]] = []  # bus events not yet recorded, with their host UTC
        self._last: dict[tuple[int, bool], LastSeen] = {}
        self._segment_seq = 0
        self._segment_row = 0  # the last segment the database has a row for
        self._segment_file: SegmentFile | None = None
        self._last_hw_us = base.hw_us
        self._frames = 0
        self._closed = False
        self.error: Exception | None = None  # the first storage failure
        self.left_open = False  # closed with something uncommitted: the next capture recovers the session

    @classmethod
    def start(
        cls,
        conn: sqlite3.Connection,
        root: DataRoot,
        *,
        run_id: str,
        vehicle_id: str,
        first: Frame,
        clock: Clock,
        mode: str = "passive",
        segment_seconds: int = SEGMENT_SECONDS,
        anchor_every: float = ANCHOR_EVERY,
    ) -> Recorder:
        """Open a session whose time base is `first`, the frame that starts it. The caller then adds it."""
        now_utc = clock.utc_now()
        base = TimeBase(first.hw_us, utc_us(now_utc))
        folder_date = now_utc.date().isoformat()
        placeholder = f"sessions/{folder_date}"
        session = capture_db.open_session(
            conn,
            run_id=run_id,
            vehicle_id=vehicle_id,
            mode=mode,
            started_utc=now_utc.isoformat(),
            folder=placeholder,
            base_hw_us=base.hw_us,
            base_utc_us=base.utc_us,
            host_monotonic=clock.monotonic(),
        )
        folder = f"{placeholder}/{session.id}"
        capture_db.set_session_folder(conn, session.key, folder)
        root.absolute(folder).mkdir(parents=True, exist_ok=True)
        return cls(
            conn, root, session, base, folder, clock, run_id=run_id, segment_seconds=segment_seconds, anchor_every=anchor_every
        )

    @property
    def session_id(self) -> str:
        return self._session.id

    @property
    def session_key(self) -> int:
        return self._session.key

    @property
    def frames(self) -> int:
        """Frames recorded so far, including any in the second being collected."""
        return self._frames

    @property
    def frames_not_written(self) -> int:
        """Frames still only in memory: after close(), frames lost to a storage failure."""
        seconds = self._ready if self._open is None else [*self._ready, self._open]
        return sum(len(second.frames) for second in seconds if second.on_disk is None)

    @property
    def frames_not_indexed(self) -> int:
        """Frames on disk that the database doesn't index yet: the next capture's recovery indexes them."""
        return sum(len(second.frames) for second in self._ready if second.on_disk is not None)

    @property
    def events_not_written(self) -> int:
        return len(self._events)

    def add(self, record: Frame | BusEvent) -> None:
        if self._closed:
            raise RuntimeError(f"session {self.session_id} is closed")
        if isinstance(record, BusEvent):
            self._events.append((self._clock.utc_now().isoformat(), record))
            self._write_events()
            return
        second = max(0, (record.hw_us - self._base.hw_us) // 1_000_000)
        current = self._open
        if current is not None and second > current.second:
            self._seal()
            current = None
        if current is None:
            current = self._open = _Second(second, self._clock.monotonic(), [])
        current.frames.append(record)
        self._frames += 1
        self._write_ready()

    def flush_due(self) -> None:
        """Write the second being collected once enough host time has passed that it must be over."""
        current = self._open
        if current is not None and self._clock.monotonic() - current.started >= FLUSH_AFTER:
            self._seal()
            self._write_ready()

    def close(self, end_reason: str) -> None:
        """Write what's left and close the session. Closing twice does nothing.

        After a storage failure this is the one more try: if anything is still uncommitted afterwards, the
        session is left open (left_open) for the next capture's recovery. It never raises for storage.
        """
        if self._closed:
            return
        self._closed = True
        if self._open is not None:
            self._seal()
        committing = True
        remaining = []
        for second in self._ready:
            try:
                self._write(second, commit=committing)
                if committing:
                    continue
            except Exception as exc:
                self._failed(exc)
                if second.on_disk is not None:
                    committing = False  # later seconds go to the segment only, after this uncommitted one
            remaining.append(second)
        self._ready = remaining
        self._write_events(retry=True)
        if self._segment_file is not None:
            self._segment_file.close()
        if not self._ready:
            try:
                capture_db.close_session(
                    self._conn,
                    self.session_id,
                    ended_utc=utc_text(self._base.utc_us_of(self._last_hw_us)),
                    end_reason=end_reason,
                )
                return
            except Exception as exc:
                self._failed(exc)
        self.left_open = True

    def _failed(self, exc: Exception) -> None:
        if self.error is None:
            self.error = exc

    def _seal(self) -> None:
        self._ready.append(self._open)  # type: ignore[arg-type]
        self._open = None

    def _write_ready(self) -> None:
        """Write and commit the sealed seconds, oldest first. After a failure, nothing until close()."""
        if self.error is not None:
            return
        while self._ready:
            try:
                self._write(self._ready[0], commit=True)
            except Exception as exc:
                self._failed(exc)
                return
            self._ready.pop(0)

    def _write_events(self, *, retry: bool = False) -> None:
        if self.error is not None and not retry:
            return
        while self._events:
            host_utc, event = self._events[0]
            try:
                capture_db.add_event(
                    self._conn,
                    run_id=self._run_id,
                    session_key=self.session_key,
                    host_utc=host_utc,
                    hw_us=event.hw_us,
                    kind=event.kind,
                    detail={"status": f"0x{event.status:05X}"},
                )
            except Exception as exc:
                self._failed(exc)
                return
            self._events.pop(0)

    def _segment_for(self, second: int) -> int:
        seq = second // self._segment_seconds + 1
        if seq != self._segment_seq:
            if self._segment_file is not None:
                self._segment_file.close()
                self._segment_file = None
            path = f"{self._folder}/seg-{seq:04d}.candump.zst"
            if self._segment_row != seq:
                # The database knows each segment before its file exists, so recovery finds every file a capture made.
                capture_db.open_segment(self._conn, session_key=self.session_key, seq=seq, path=path)
                self._segment_row = seq
            self._segment_file = SegmentFile(self._root.absolute(path), self._base)
            self._segment_seq = seq
        return seq

    def _write(self, second: _Second, *, commit: bool) -> None:
        """Put one second on disk if it isn't yet, then, with `commit`, record it. State changes only once it has."""
        frames = second.frames
        if second.on_disk is None:
            seq = self._segment_for(second.second)
            offset, length = self._segment_file.write_second(second.second, frames)  # type: ignore[union-attr]
            second.on_disk = (seq, offset, length)
        if not commit:
            return
        seq, offset, length = second.on_disk
        errors = sum(frame.error for frame in frames)
        row = capture_db.SecondRow(
            self.session_key, second.second, seq, offset, length, len(frames), errors, frames[0].hw_us, frames[-1].hw_us
        )
        last = dict(self._last)
        ids = summarize(frames, last)
        now = self._clock.monotonic()
        anchor = None
        if now - self._last_anchor >= self._anchor_every:
            anchor = capture_db.Anchor(frames[-1].hw_us, self._clock.utc_now().isoformat(), now)
        capture_db.record_second(self._conn, row, ids, anchor)
        self._last = last
        if anchor is not None:
            self._last_anchor = now
        self._last_hw_us = max(self._last_hw_us, frames[-1].hw_us)
