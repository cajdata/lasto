r"""Where lasto keeps its data, and every path under it (docs/architecture.md §0 and §14.3).

The data folder is %LOCALAPPDATA%\lasto unless --data or LASTO_DATA names another. Paths stored in
the databases are relative to it, with forward slashes, so a copied folder works on any machine.

The folder is typed by the user, and SQLite opens its files itself, where the serial guard's audit
hook (which sees open() and _winapi.CreateFile) can't see them. So a folder that names a device is
refused here: a serial port, the device namespace, or any other Windows reserved name.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

FOLDER_NAME = "lasto"
# Windows reserved device names (COM1 and AUX are serial ports), and GLOBALROOT, which reaches any device.
_RESERVED = re.compile(r"(?i)con|prn|aux|nul|com[0-9¹²³]+|lpt[0-9¹²³]+|conin\$|conout\$|globalroot")
# The device namespaces: two backslashes then a dot or a question mark, or backslash, two question marks.
_DEVICE_NAMESPACE = re.compile(r"^(?:\\\\[.?]|\\\?\?)\\")


def names_a_device(path: str) -> bool:
    """Whether a path could reach a device rather than a file: a device namespace, or a reserved name anywhere."""
    text = path.replace("/", "\\")
    if _DEVICE_NAMESPACE.match(text):
        return True
    for part in text.split("\\"):
        for piece in part.split(":"):  # C:COM5 names COM5, on drive C
            if _RESERVED.fullmatch(piece.split(".")[0].rstrip(" ")):
                return True
    return False


def stored_path_problem(stored: str) -> str | None:
    """Why a path stored in a database is unusable, or None: it is relative, with forward slashes, under the root."""
    if not stored:
        return "is empty"
    if "\\" in stored:
        return "uses backslashes"
    pure = PurePosixPath(stored)
    if pure.is_absolute() or ":" in stored:
        return "isn't relative to the data folder"
    if ".." in pure.parts:
        return "leaves the data folder"
    return None


class DataRoot:
    """The data folder, and the files and folders lasto keeps in it."""

    def __init__(self, path: Path | str) -> None:
        if names_a_device(str(path)):
            raise ValueError(f"{str(path)!r} names a device, not a folder for lasto's data")
        self.path = Path(path).resolve()

    @classmethod
    def locate(cls, explicit: str | None, *, environ: Mapping[str, str] = os.environ) -> DataRoot:
        """--data first, then LASTO_DATA, then %LOCALAPPDATA%\\lasto."""
        if explicit:
            return cls(explicit)
        if environ.get("LASTO_DATA"):
            return cls(environ["LASTO_DATA"])
        local = environ.get("LOCALAPPDATA")
        return cls((Path(local) if local else Path.home() / "AppData" / "Local") / FOLDER_NAME)

    @property
    def capture_db(self) -> Path:
        return self.path / "capture.sqlite"

    @property
    def workbench_db(self) -> Path:
        return self.path / "workbench.sqlite"

    @property
    def live_db(self) -> Path:
        return self.path / "live.sqlite"

    @property
    def audit_dir(self) -> Path:
        return self.path / "audit"

    @property
    def sessions_dir(self) -> Path:
        return self.path / "sessions"

    @property
    def lock_file(self) -> Path:
        return self.path / "capture.lock"

    @property
    def stop_file(self) -> Path:
        return self.path / "capture.stop"

    def ensure(self) -> None:
        for folder in (self.path, self.audit_dir, self.sessions_dir):
            folder.mkdir(parents=True, exist_ok=True)

    def relative(self, path: Path) -> str:
        """How a path under the data folder is stored in a database."""
        try:
            return Path(path).resolve().relative_to(self.path).as_posix()
        except ValueError:
            raise ValueError(f"{path} is outside the data folder {self.path}") from None

    def absolute(self, stored: str) -> Path:
        """Where a path stored in a database is on this machine."""
        problem = stored_path_problem(stored)
        if problem is not None:
            raise ValueError(f"stored path {stored!r} {problem}")
        return self.path.joinpath(*PurePosixPath(stored).parts)
