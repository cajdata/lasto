"""The data folder, for the CLI and later the GUI (docs/architecture.md §4)."""

from __future__ import annotations

from lasto.storage.root import DataRoot


def data_root(explicit: str | None) -> DataRoot:
    """--data first, then LASTO_DATA, then %LOCALAPPDATA%\\lasto. A folder that names a device is refused."""
    return DataRoot.locate(explicit)
