"""Retention: how much disk lasto's data may use, and the warnings a capture gives when it starts (docs/architecture.md §4).

The owner's decisions (2026-10-01):
- Keep everything through Phase 5: early drives are the reference for mapping and decoding.
- After that, a budget of 20 GB for the data folder. Over it, raw segments go oldest first, down to 90%
  of the budget. Never the newest 30 days, and never a session that's open, tagged, has notes, or is
  referenced by mapping work. The database rows stay.
- Deletion happens only by command or in the GUI, under the capture lock, in small batches, audited.
  Never automatically, and never during `drive`. That part isn't built yet.
- `drive` warns when it starts if the disk has less than 2 GB free, or the data folder is over its
  budget. A warning never stops a capture: a disk that actually fills is a storage failure, which the
  capture already handles (review finding L8).

Sizes are in GiB, as Windows shows them ("GB").
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lasto.storage.root import DataRoot

GIB = 2**30
BUDGET_BYTES = 20 * GIB
LOW_FREE_BYTES = 2 * GIB
NEWEST_KEPT_DAYS = 30  # for pruning, after Phase 5


@dataclass(frozen=True)
class DiskCheck:
    free_bytes: int  # on the disk that holds the data folder
    used_bytes: int  # by the data folder
    warnings: tuple[str, ...]
    budget_bytes: int = BUDGET_BYTES

    def summary(self) -> str:
        return (
            f"{self.free_bytes / GIB:.1f} GB free on the data folder's disk; the data folder uses"
            f" {self.used_bytes / GIB:.1f} GB of its {self.budget_bytes / GIB:.0f} GB budget."
        )


def data_folder_size(root: DataRoot) -> int:
    """Every file under the data folder, in bytes. A file that goes away while counting is skipped."""
    total = 0
    for folder, _subfolders, files in os.walk(root.path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(folder, name))
            except OSError:
                continue
    return total


def check_disk(
    root: DataRoot,
    *,
    disk_usage: Callable[[str], Any] | None = None,
    budget_bytes: int = BUDGET_BYTES,
    low_free_bytes: int = LOW_FREE_BYTES,
) -> DiskCheck:
    """How much room a capture has, with a warning for a nearly full disk or a data folder over its budget."""
    free = (shutil.disk_usage if disk_usage is None else disk_usage)(str(root.path)).free
    used = data_folder_size(root)
    warnings = []
    if free < low_free_bytes:
        warnings.append(
            f"only {free / GIB:.1f} GB free on the data folder's disk, below {low_free_bytes / GIB:.0f} GB;"
            " the capture goes on, at about 20 MB an hour"
        )
    if used > budget_bytes:
        warnings.append(
            f"the data folder uses {used / GIB:.1f} GB, over its budget of {budget_bytes / GIB:.0f} GB;"
            " nothing is deleted automatically, and pruning comes after Phase 5"
        )
    return DiskCheck(free, used, tuple(warnings), budget_bytes)
