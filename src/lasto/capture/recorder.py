"""Records one session: its raw frames to segment files, and each second to the capture database.

Raw frames reach disk first (docs/architecture.md §1, rule 2 of the system's shape):
- A second is written to its segment and fsynced before its database transaction commits, so the
  database never points at data that isn't on disk.
- A crash costs at most the second being written: a second written but not committed is found and
  indexed by the next capture's recovery (lasto.capture.recovery).

Time:
- A second is a whole second of hardware time from the session's first frame.
- Candump timestamps come from the session's time base: that first frame's hardware timestamp, and
  the UTC it arrived at.
- A clock anchor (hardware timestamp, host UTC, host monotonic) is recorded every minute, for drift.

A new segment file starts every hour of the session.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
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
class _Pending:
    second: int
    started: float  # host monotonic time its first frame arrived
    frames: list[Frame]


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
        self._pending: _Pending | None = None
        self._last: dict[tuple[int, bool], LastSeen] = {}
        self._segment_seq = 0
        self._segment_file: SegmentFile | None = None
        self._last_hw_us = base.hw_us
        self._frames = 0
        self._closed = False

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

    def add(self, record: Frame | BusEvent) -> None:
        if self._closed:
            raise RuntimeError(f"session {self.session_id} is closed")
        if isinstance(record, BusEvent):
            capture_db.add_event(
                self._conn,
                run_id=self._run_id,
                session_key=self.session_key,
                host_utc=self._clock.utc_now().isoformat(),
                hw_us=record.hw_us,
                kind=record.kind,
                detail={"status": f"0x{record.status:05X}"},
            )
            return
        second = max(0, (record.hw_us - self._base.hw_us) // 1_000_000)
        pending = self._pending
        if pending is not None and second > pending.second:
            self._write(pending)
            pending = None
        if pending is None:
            pending = self._pending = _Pending(second, self._clock.monotonic(), [])
        pending.frames.append(record)
        self._frames += 1

    def flush_due(self) -> None:
        """Write the second being collected once enough host time has passed that it must be over."""
        pending = self._pending
        if pending is not None and self._clock.monotonic() - pending.started >= FLUSH_AFTER:
            self._write(pending)

    def close(self, end_reason: str) -> None:
        """Write what's left and close the session. Closing twice does nothing."""
        if self._closed:
            return
        if self._pending is not None:
            self._write(self._pending)
        if self._segment_file is not None:
            self._segment_file.close()
        self._closed = True
        capture_db.close_session(
            self._conn, self.session_id, ended_utc=utc_text(self._base.utc_us_of(self._last_hw_us)), end_reason=end_reason
        )

    def _segment_for(self, second: int) -> int:
        seq = second // self._segment_seconds + 1
        if seq != self._segment_seq:
            if self._segment_file is not None:
                self._segment_file.close()
            path = f"{self._folder}/seg-{seq:04d}.candump.zst"
            # The database knows each segment before its file exists, so recovery finds every file a capture made.
            capture_db.open_segment(self._conn, session_key=self.session_key, seq=seq, path=path)
            self._segment_file = SegmentFile(self._root.absolute(path), self._base)
            self._segment_seq = seq
        return seq

    def _write(self, pending: _Pending) -> None:
        self._pending = None
        frames = pending.frames
        seq = self._segment_for(pending.second)
        offset, length = self._segment_file.write_second(pending.second, frames)  # type: ignore[union-attr]
        errors = sum(frame.error for frame in frames)
        row = capture_db.SecondRow(
            self.session_key, pending.second, seq, offset, length, len(frames), errors, frames[0].hw_us, frames[-1].hw_us
        )
        now = self._clock.monotonic()
        anchor = None
        if now - self._last_anchor >= self._anchor_every:
            anchor = capture_db.Anchor(frames[-1].hw_us, self._clock.utc_now().isoformat(), now)
            self._last_anchor = now
        capture_db.record_second(self._conn, row, summarize(frames, self._last), anchor)
        self._last_hw_us = max(self._last_hw_us, frames[-1].hw_us)
