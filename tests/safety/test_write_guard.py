r"""Guard v2 (Phase 3): a process writes only inside the folders it registered, its data folder.

The old guard refused writes to paths that looked like serial devices, and three path forms got past it
(\\?\USB#..., \\?\BTHENUM#..., C:COM5). Guard v2 turns that around: every write-capable open must name a
regular file, or a new one in a real folder, inside an allowed root. Every other path form fails the first
check without being listed anywhere. Nothing here opens a device: the decision runs lexically first, and the
paths that would name one are refused before anything touches the filesystem.
"""

from __future__ import annotations

import _winapi
import os
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import events

import lasto
from lasto.safety import serial_guard
from lasto.safety.audit import REFUSALS
from lasto.safety.errors import SafetyViolation
from lasto.safety.serial_guard import WriteGuard, write_problem

REPO = Path(lasto.__file__).resolve().parents[2]
CREATE_NEW = 1  # CreateFile's creation disposition; _winapi has no constant for it


@pytest.fixture
def root(tmp_path) -> Path:
    folder = tmp_path / "data"
    folder.mkdir()
    return folder


def roots(folder: Path) -> tuple[str, ...]:
    return (str(folder),)


# ---- the decision ----


def test_a_new_or_existing_regular_file_in_the_root_is_allowed(root):
    (root / "audit").mkdir()
    (root / "capture.sqlite").write_bytes(b"")
    assert write_problem(str(root / "capture.sqlite"), roots(root)) is None
    assert write_problem(root / "audit" / "run.jsonl", roots(root)) is None  # new, in a real folder
    assert write_problem(os.fsencode(root / "live.sqlite"), roots(root)) is None
    assert write_problem(3, roots(root)) is None  # an open descriptor: its path was checked when it opened


@pytest.mark.parametrize(
    "path",
    [
        r"\\.\COM5",
        r"\\?\COM5",
        "COM5",
        r"\\?\USB#VID_0403&PID_6001#A10K1234#{86e0d1e0-8089-11d0-9ce4-08003e301f73}",  # a device interface path
        r"\\?\BTHENUM#{00001101-0000-1000-8000-00805f9b34fb}_LOCALMFG&0002#7&1#0",  # a Bluetooth serial port
        r"\\.\GLOBALROOT\Device\Serial0",
        r"\\server\share\file.bin",
        r"C:\Windows\Temp\file.bin",
    ],
)
def test_every_path_outside_the_root_is_refused_without_being_listed(root, path):
    assert write_problem(path, roots(root)) == "outside the folders this process may write"


def test_the_extended_length_form_of_a_path_in_the_root_is_the_same_path(root):
    r"""pytest's temp-folder cleanup writes through \\?\C:\..., which Windows takes literally: no rewriting."""
    assert write_problem("\\\\?\\" + str(root / "lock"), roots(root)) is None
    assert write_problem("\\\\?\\" + str(root) + "\\COM5", roots(root)) == "'COM5' isn't a plain file or folder name"
    assert write_problem("\\\\?\\" + str(root) + "\\a\\..\\b", roots(root)) == "'..' isn't a plain file or folder name"
    assert write_problem("\\\\?\\UNC\\server\\share\\file", roots(root)) == "outside the folders this process may write"


def test_a_drive_relative_name_is_made_absolute_first(root, monkeypatch):
    """C:COM5 got past the old guard. Here it becomes <cwd>\\COM5, and a reserved name in the root is refused."""
    monkeypatch.chdir(root)
    drive = str(root)[:2]
    assert write_problem(f"{drive}COM5", roots(root)) == "'COM5' isn't a plain file or folder name"
    assert write_problem(f"{drive}notes.txt", roots(root)) is None


@pytest.mark.parametrize(
    "name",
    ["COM5", "com5.txt", "com5 .txt", "nul.log", "AUX", "CON", "PRN", "LPT1", "COM¹", "CONIN$", "conout$.log",
     "file.txt:stream", "a<b"],
)  # fmt: skip
def test_a_name_windows_would_send_to_a_device_or_read_as_a_stream_is_refused(root, name):
    """Windows reads a name's base as everything before the first dot, trailing spaces dropped: com5 .txt is COM5."""
    assert write_problem(str(root) + "\\" + name, roots(root)) == f"{name!r} isn't a plain file or folder name"


@pytest.mark.parametrize("name", ["NUL", "COM5 ", ""])
def test_a_name_win32_rewrites_is_refused_whatever_it_becomes(root, name):
    """GetFullPathName, which abspath uses, rewrites these the way CreateFile would, and how depends on the
    Windows build: NUL becomes \\\\.\\NUL (outside the root); COM5 with a trailing space loses the space (a
    reserved name); an empty name becomes the root itself (outside). Each is refused either way."""
    assert write_problem(str(root) + "\\" + name, roots(root)) is not None


@pytest.mark.parametrize("name", ["trailing.", "trailing "])
def test_a_trailing_dot_or_space_is_dropped_by_win32_so_the_plain_name_is_what_gets_written(root, name):
    """CreateFile writes 'trailing' for these, and so does the check: the same plain file."""
    assert os.path.abspath(str(root) + "\\" + name) == str(root / "trailing")
    assert write_problem(str(root) + "\\" + name, roots(root)) is None


def test_a_reserved_name_deeper_down_is_refused_too(root):
    (root / "sessions").mkdir()
    assert write_problem(root / "sessions" / "aux" / "seg.zst", roots(root)) == "'aux' isn't a plain file or folder name"


def test_a_junction_inside_the_root_is_refused(root, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _winapi.CreateJunction(str(elsewhere), str(root / "link"))
    assert write_problem(root / "link" / "file.bin", roots(root)) == "a link or junction leads elsewhere"


def test_only_a_regular_file_or_a_new_one_in_a_real_folder(root):
    (root / "folder").mkdir()
    assert write_problem(root / "folder", roots(root)) == "not a regular file"
    assert write_problem(root / "missing" / "file.bin", roots(root)) == "its folder doesn't exist"
    (root / "plain").write_bytes(b"")
    assert write_problem(root / "plain" / "file.bin", roots(root)) == "its folder isn't a folder"


def test_the_root_itself_and_its_parent_are_not_inside_it(root):
    assert write_problem(root, roots(root)) == "outside the folders this process may write"
    assert write_problem(str(root) + "x\\file.bin", roots(root)) == "outside the folders this process may write"
    assert write_problem(root / ".." / "file.bin", roots(root)) == "outside the folders this process may write"


def test_something_that_is_not_a_path_is_refused(root):
    assert write_problem(object(), roots(root)) == "not a path"


# ---- registering the data folder ----


def test_the_data_folder_is_registered_once(root, tmp_path):
    guard = WriteGuard()
    guard.allow(root, more_than_one=False)
    guard.allow(str(root), more_than_one=False)  # the same folder again: nothing changes
    assert guard.roots == (str(root),)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(SafetyViolation) as refused:
        guard.allow(other, more_than_one=False)
    assert refused.value.reason == "write_folder_already_set"
    guard.allow(other, more_than_one=True)  # only a test run may add more
    assert guard.roots == (str(root), str(other))


@pytest.mark.parametrize(
    ("folder", "problem"),
    [
        (r"\\.\COM5", "not a folder on a drive"),
        (r"\\server\share\lasto", "not a folder on a drive"),
        ("missing", "not an existing folder"),
        ("C:\\", "isn't a plain file or folder name"),  # a whole drive is not a data folder
    ],
)
def test_only_a_real_folder_on_a_drive_can_be_registered(tmp_path, folder, problem):
    path = folder if folder.startswith("\\") else str(tmp_path / folder)
    with pytest.raises(SafetyViolation, match=problem) as refused:
        WriteGuard().allow(path, more_than_one=False)
    assert refused.value.reason == "write_folder_refused"


def test_a_registered_folder_cannot_be_a_reserved_name_or_a_link(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    _winapi.CreateJunction(str(target), str(tmp_path / "link"))
    with pytest.raises(SafetyViolation, match="a link or junction"):
        WriteGuard().allow(tmp_path / "link", more_than_one=False)
    with pytest.raises(SafetyViolation, match="isn't a plain"):
        WriteGuard().allow(str(tmp_path) + "\\com5.txt", more_than_one=False)
    with pytest.raises(SafetyViolation, match="not a folder on a drive"):
        WriteGuard().allow(str(tmp_path) + "\\NUL", more_than_one=False)  # Win32 makes it \\.\NUL
    plain = tmp_path / "plain.txt"
    plain.write_bytes(b"")
    with pytest.raises(SafetyViolation, match="not an existing folder"):
        WriteGuard().allow(plain, more_than_one=False)


# ---- the hook ----


OUTSIDE = str(REPO / "lasto-write-guard-probe.bin")  # the repo itself isn't a folder tests may write in


@pytest.mark.parametrize(
    ("event", "args"),
    [
        ("open", (OUTSIDE, "w", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_BINARY | os.O_NOINHERIT)),
        ("open", (OUTSIDE, None, os.O_RDWR)),  # os.open
        ("open", (OUTSIDE, None, os.O_RDONLY | os.O_APPEND)),
        ("open", (OUTSIDE, "a", None)),  # no flags: the mode decides
        ("open", (OUTSIDE, None, None)),  # neither: a write, to be safe
        ("os.truncate", (OUTSIDE, 0)),
        ("_winapi.CreateFile", (OUTSIDE, _winapi.GENERIC_WRITE, 0, _winapi.OPEN_EXISTING, 0)),
        ("_winapi.CreateFile", (OUTSIDE, _winapi.GENERIC_READ, 0, CREATE_NEW, 0)),  # creates a file
        ("_winapi.CreateFile", (OUTSIDE, None, 0, _winapi.OPEN_EXISTING, 0)),
    ],
)
def test_the_hook_refuses_and_audits_a_write_outside_the_allowed_folders(auditor, sink, event, args):
    REFUSALS.attach(auditor)
    with pytest.raises(SafetyViolation) as refused:
        serial_guard.guard_event(event, args)
    assert refused.value.reason == "write_outside_the_data_folder"
    [refusal] = events(sink, "rejected")
    assert (refusal["reason"], refusal["transport"]) == ("write_outside_the_data_folder", "core")
    assert "outside the folders this process may write" in refusal["detail"]


def test_the_hook_lets_reads_through_and_writes_inside(auditor, sink, tmp_path):
    REFUSALS.attach(auditor)
    serial_guard.guard_event("open", (OUTSIDE, "r", os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT))
    serial_guard.guard_event("_winapi.CreateFile", (OUTSIDE, _winapi.GENERIC_READ, 0, _winapi.OPEN_EXISTING, 0))
    serial_guard.guard_event("open", (str(tmp_path / "f.bin"), "w", os.O_WRONLY | os.O_CREAT))  # a test folder
    serial_guard.guard_event("os.listdir", (OUTSIDE,))  # not an open
    assert events(sink, "rejected") == []


def test_a_real_write_outside_is_refused_before_the_file_exists():
    with pytest.raises(SafetyViolation):
        open(OUTSIDE, "w")
    with pytest.raises(SafetyViolation):
        Path(OUTSIDE).write_bytes(b"x")
    assert not Path(OUTSIDE).exists()


def test_a_real_write_inside_a_test_folder_works(tmp_path):
    (tmp_path / "ok.bin").write_bytes(b"x")
    assert (tmp_path / "ok.bin").read_bytes() == b"x"


PRODUCTION = r"""
import os, sys, tempfile
import lasto.safety
from lasto.safety.errors import SafetyViolation
from lasto.safety.serial_guard import allow_writes_in

folder = sys.argv[1]
def tries(path):
    try:
        with open(path, "w") as file:
            file.write("x")
        return "wrote"
    except SafetyViolation as exc:
        return exc.reason

print(tries(os.path.join(folder, "before.txt")))  # nothing registered yet
allow_writes_in(folder)
print(tries(os.path.join(folder, "after.txt")))
os.mkdir(os.path.join(folder, "other"))
try:
    allow_writes_in(os.path.join(folder, "other"))
    print("added")
except SafetyViolation as exc:
    print(exc.reason)
"""


def test_outside_a_test_run_a_process_writes_only_to_the_one_folder_it_registered(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    done = subprocess.run(
        [sys.executable, "-c", PRODUCTION, str(folder)], capture_output=True, text=True, check=True
    )
    assert done.stdout.split() == ["write_outside_the_data_folder", "wrote", "write_folder_already_set"]
    assert not (folder / "before.txt").exists() and (folder / "after.txt").read_text() == "x"
