"""Numbers the docs quote from the app outside the safety core, read from source like the safety facts.

Parsed with ast and never imported or run, with the same rules as sitegen/safety.py: only single
literal constants, refused if any statement in the file binds the name again.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sitegen import paths
from sitegen.data import BuildError
from sitegen.safety import _count, _num, _read

# The files read, relative to src/lasto. A page that quotes these numbers dates from them too.
FILES = ["operations/drive.py", "capture/recorder.py", "storage/retention.py", "cli.py"]


@dataclass(frozen=True)
class CaptureFacts:
    silence: float  # seconds without a frame that end a session
    poll: float  # seconds between reads of the channel
    tick: float  # seconds between audit catch-ups, live feed writes, and stop-file checks
    progress: float  # seconds between progress lines
    flush_after: float  # seconds before a quiet second is written anyway
    segment_seconds: int  # seconds of traffic per segment file
    anchor_every: float  # seconds between clock anchors
    budget_gb: int  # the data folder's budget, in GiB
    low_free_gb: int  # free space below which drive warns, in GiB
    newest_kept_days: int  # days pruning will never touch (pruning comes after Phase 5)
    simulated_seconds: float  # how long a simulated lasto drive runs by default
    poll_ms: int  # poll, in whole milliseconds, as the docs give it
    segment_minutes: int  # segment_seconds, in whole minutes, as the docs give it


def _gib(value: object, gib: object, what: str) -> int:
    return _count(_num(value, what) / _num(gib, "GIB"), what)


def load_capture_facts(root: Path | None = None) -> CaptureFacts:
    base = root or paths.ROOT
    app = base / "src" / "lasto"
    drive, recorder, retention, cli = (_read(app / rel, base) for rel in FILES)
    gib = retention.get("GIB")
    if gib != 2**30:
        raise BuildError(f"retention.py's GIB is {gib}, not 2**30; the docs call the budget GB")
    poll = _num(drive.get("POLL"), "POLL")
    segment_seconds = _count(recorder.get("SEGMENT_SECONDS"), "SEGMENT_SECONDS")
    return CaptureFacts(
        silence=_num(drive.get("SILENCE"), "SILENCE"),
        poll=poll,
        tick=_num(drive.get("TICK"), "TICK"),
        progress=_num(drive.get("PROGRESS"), "PROGRESS"),
        flush_after=_num(recorder.get("FLUSH_AFTER"), "FLUSH_AFTER"),
        segment_seconds=segment_seconds,
        anchor_every=_num(recorder.get("ANCHOR_EVERY"), "ANCHOR_EVERY"),
        budget_gb=_gib(retention.get("BUDGET_BYTES"), gib, "BUDGET_BYTES"),
        low_free_gb=_gib(retention.get("LOW_FREE_BYTES"), gib, "LOW_FREE_BYTES"),
        newest_kept_days=_count(retention.get("NEWEST_KEPT_DAYS"), "NEWEST_KEPT_DAYS"),
        simulated_seconds=_num(cli.get("SIMULATED_SECONDS"), "SIMULATED_SECONDS"),
        # Shown as whole numbers, so a value that isn't one fails the build instead of being rounded.
        poll_ms=_count(round(poll * 1000, 9), "POLL (in ms)"),
        segment_minutes=_count(segment_seconds / 60, "SEGMENT_SECONDS (in minutes)"),
    )
