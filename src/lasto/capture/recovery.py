"""Recovery at capture start: closes what a crash or power loss left open (docs/architecture.md §4).

Only a capture process runs it, holding the capture lock, so nothing else is writing.

A session left open:
- Its database rows index a prefix of each segment file: every second is fsynced to its segment before
  its row commits. So what can be missing is the second being written when the process stopped, and a
  torn tail after it.
- Each segment is read from where its index ends. Every complete second found there, in order, is
  indexed with its rollups, just as the recorder would have indexed it.
- The rest, a torn tail, is moved to a `.torn` file beside the segment. The segment then reads cleanly
  to its end, and no byte that reached the disk is thrown away.
- A segment shorter than its index lost data the database says was on disk. It's reported, and left
  as it is.
- The session is closed as recovered, ending at its last indexed frame.

A run left unended: its audit log is indexed to the end, and the run is ended at the last time it's
known to have reached.

Running recovery again after it was cut short finishes the job: every step is either one transaction
or safe to repeat.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from lasto.capture.recorder import utc_text
from lasto.capture.rollups import LastSeen, summarize
from lasto.storage import capture_db
from lasto.storage.audit_index import AuditIndex
from lasto.storage.root import DataRoot
from lasto.storage.segments import TimeBase, read_segment

TORN_SUFFIX = ".torn"


@dataclass(frozen=True, slots=True)
class RecoveredSession:
    id: str
    seconds_indexed: int
    bytes_set_aside: int
    damaged_segments: tuple[str, ...]  # segments shorter than their index, as stored paths


@dataclass(frozen=True, slots=True)
class Recovery:
    sessions: tuple[RecoveredSession, ...]
    runs: tuple[str, ...]  # runs ended


def recover(conn: sqlite3.Connection, root: DataRoot, *, now_utc: str) -> Recovery:
    """Close every session and run a crash or power loss left open."""
    sessions = tuple(_recover_session(conn, root, left, now_utc) for left in capture_db.sessions_left_open(conn))
    runs = tuple(_end_run(conn, root, run) for run in capture_db.runs_left_unended(conn))
    return Recovery(sessions, runs)


def _recover_session(conn: sqlite3.Connection, root: DataRoot, left: capture_db.LeftOpen, now_utc: str) -> RecoveredSession:
    base = TimeBase(left.base_hw_us, left.base_utc_us)
    last = {key: LastSeen(hw_us, data) for key, (hw_us, data) in capture_db.last_frames(conn, left.key).items()}
    last_second, last_hw_us = left.last_second, left.last_hw_us
    indexed = set_aside = 0
    damaged: list[str] = []
    for segment in capture_db.segments_of(conn, left.key):
        path = root.absolute(segment.path)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size < segment.stored_bytes:
            damaged.append(segment.path)
            continue
        if size == segment.stored_bytes:
            continue
        with open(path, "rb") as file:
            file.seek(segment.stored_bytes)
            tail = file.read()
        seconds, _ = read_segment(tail, base)
        kept = 0
        for second in seconds:
            if last_second is not None and second.seq <= last_second:
                break  # a whole second, but out of order: not the recorder's, so not trusted
            frames = second.frames
            row = capture_db.SecondRow(
                left.key, second.seq, segment.seq, segment.stored_bytes + second.offset, second.length, len(frames),
                sum(frame.error for frame in frames), second.first_hw_us, second.last_hw_us,
            )  # fmt: skip
            capture_db.record_second(conn, row, summarize(frames, last), None)
            last_second = second.seq
            last_hw_us = second.last_hw_us if last_hw_us is None else max(last_hw_us, second.last_hw_us)
            kept = second.offset + second.length
            indexed += 1
        if kept < len(tail):
            _set_aside(path, segment.stored_bytes + kept, tail[kept:])
            set_aside += len(tail) - kept
    ended = left.started_utc if last_hw_us is None else utc_text(base.utc_us_of(last_hw_us))
    detail: dict[str, object] = {
        "seconds_indexed": indexed,
        "bytes_set_aside": set_aside,
        "damaged_segments": damaged,
    }
    capture_db.close_recovered(conn, left, ended_utc=ended, host_utc=now_utc, detail=detail)
    return RecoveredSession(left.id, indexed, set_aside, tuple(damaged))


def _set_aside(path: Path, keep: int, tail: bytes) -> None:
    """Move a segment's tail to a file beside it, then cut the segment after its last good second."""
    aside = path.with_name(path.name + TORN_SUFFIX)
    with open(aside, "wb") as file:  # a recovery cut short wrote the same bytes here before
        file.write(tail)
        file.flush()
        os.fsync(file.fileno())
    with open(path, "r+b") as file:
        file.truncate(keep)
        file.flush()
        os.fsync(file.fileno())


def _end_run(conn: sqlite3.Connection, root: DataRoot, run: capture_db.LeftUnended) -> str:
    AuditIndex(conn, run.id, root.absolute(run.audit_path)).catch_up()
    ended = max(capture_db.run_times(conn, run.id), key=datetime.fromisoformat)
    capture_db.end_run(conn, run.id, ended_utc=ended, end_reason="interrupted")
    return run.id
