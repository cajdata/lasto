"""The data folder, as a lasto process uses it (docs/architecture.md §3.7 and §4).

Guard v2 lets a process write only inside the one folder it registered, and SQLite opens nothing outside it
either, even to read. So every command that touches the data folder registers it first.
"""

from __future__ import annotations

from lasto.safety.audit import hold_on_disk
from lasto.safety.serial_guard import allow_writes_in
from lasto.storage.root import DataRoot


def use_data_folder(root: DataRoot) -> None:
    """For a command that writes (drive). Once per process, before anything else writes:
    - this process may write only inside the data folder (guard v2, lasto.safety.serial_guard);
    - refusals recorded while no audit log is attached also go to `audit/held.jsonl` there, fsynced as they're
      held, so a crash can't lose one the file took. One it couldn't take (a full disk) stays in memory,
      counted, and is reported at exit."""
    root.ensure()
    allow_writes_in(root.path)
    hold_on_disk(root.audit_dir / "held.jsonl")


def read_data_folder(root: DataRoot) -> None:
    """For a command that only reads (log). SQLite connects only inside a registered folder, even read-only, so
    the data folder is registered if it exists. A reader creates nothing: with no data folder there's nothing to
    open."""
    if root.path.is_dir():
        allow_writes_in(root.path)
