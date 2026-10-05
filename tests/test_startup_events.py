"""Before the safety core is imported, a lasto process writes nothing, loads no native code, and connects to
nothing (Phase 3, A6).

cli.main imports the safety core, which installs guard v2, before it dispatches a command (test_layers.py). This
checks the time before that. A sitecustomize on PYTHONPATH records every audit event from site initialization, the
earliest point Python runs code from outside its own startup, until the safety core's import begins. That's done for
the lasto console script and for python -m lasto, with a command that reads (log) and one that captures (drive, in
the simulator). None of the events may be a write-capable open or any other change to a file, a ctypes load or
lookup, a socket or SQLite connection, a child process, or an import of a module that creates subinterpreters (the
guard refuses that import, but only a first import raises the event), or of asyncio or concurrent.futures, which load
one on Python 3.14.

Python's own bytecode cache is off in these processes (PYTHONDONTWRITEBYTECODE): an install built with
`uv sync --locked --compile-bytecode` leaves it nothing to write, and in a working tree a stale cache file would be
Python's write, not lasto's. The safety core's own import binds kernel32's GetSystemDirectoryW before its hook goes
in; that's the core itself, after the window this checks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

RECORDER = r"""
import json, sys

_seen = []
_recording = [True]


def _simple(value):
    return value if value is None or isinstance(value, (str, int)) else repr(value)


def _record(event, args):
    if not _recording[0]:
        return
    if event == "import" and args[0] == "lasto.safety":
        _recording[0] = False
        sys.stderr.write("LASTO-EVENTS " + json.dumps(_seen) + "\n")
        sys.stderr.flush()
        return
    _seen.append([event, [_simple(arg) for arg in args[:3]]])


sys.addaudithook(_record)
"""

ENTRY_POINTS = {
    "console script": [str(Path(sys.executable).with_name("lasto.exe"))],
    "python -m lasto": [sys.executable, "-m", "lasto"],
}

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
# Audit events that change a file or folder without an open, and the ones that start a process.
_CHANGES = {
    "os.truncate", "os.remove", "os.rmdir", "os.rename", "os.mkdir", "os.chmod", "os.utime", "os.link", "os.symlink",
    "shutil.copyfile", "shutil.copymode", "shutil.copystat", "shutil.copytree", "shutil.move", "shutil.rmtree",
    "subprocess.Popen", "os.system", "os.startfile", "os.startfile/2", "os.spawn", "os.exec",
}  # fmt: skip
# Native code (ctypes), the network, SQLite, and Win32 handles (_winapi.CreateFile, CreateProcess, and the like).
_PREFIXES = ("ctypes.", "socket.", "sqlite3.", "_winapi.")
# The guard refuses importing these (they create subinterpreters), but only a first import raises the event, so none
# may be imported before the guard is in. Nor may what loads one on Python 3.14: asyncio imports concurrent.futures,
# which tries _interpreters as it loads. On 3.13 neither loads it, so without them here only a 3.14 run would catch a
# dependency that starts importing asyncio before the core.
_SUBINTERPRETER_MODULES = {"_interpreters", "_xxsubinterpreters", "_testcapi", "_testinternalcapi"}
_LOADS_ONE_ON_3_14 = {"asyncio", "concurrent.futures", "concurrent.interpreters"}


def forbidden(event: str, args: list[object]) -> bool:
    if event == "import":
        return args[0] in _SUBINTERPRETER_MODULES | _LOADS_ONE_ON_3_14
    if event == "open":
        mode, flags = args[1], args[2]
        writes = isinstance(mode, str) and any(letter in mode for letter in "wax+")
        return writes or (isinstance(flags, int) and bool(flags & _WRITE_FLAGS))
    return event in _CHANGES or event.startswith(_PREFIXES)


@pytest.mark.parametrize(
    ("event", "args", "flagged"),
    [
        ("open", ["C:\\x.txt", "w", 0], True),
        ("open", ["C:\\x.txt", None, os.O_WRONLY | os.O_CREAT], True),
        ("open", ["C:\\x.txt", "r+b", 0], True),
        ("open", ["C:\\lib\\x.py", "rb", os.O_RDONLY], False),
        ("open", ["C:\\lib\\x.py", None, os.O_RDONLY | os.O_BINARY], False),
        ("ctypes.dlopen", ["kernel32"], True),
        ("socket.connect", ["<socket>", "('1.2.3.4', 80)"], True),
        ("sqlite3.connect", [":memory:"], True),
        ("_winapi.CreateFile", ["x", 0, 0], True),
        ("os.mkdir", ["C:\\x", 511, None], True),
        ("subprocess.Popen", ["x", "x", None], True),
        ("import", ["lasto.cli", None, "[]"], False),
        ("import", ["_interpreters", None, "[]"], True),
        ("import", ["_testcapi", None, "[]"], True),
        ("import", ["asyncio", None, "[]"], True),
        ("import", ["concurrent.futures", None, "[]"], True),
        ("import", ["concurrent.interpreters", None, "[]"], True),
        ("import", ["concurrent", None, "[]"], False),
        ("import", ["asyncio_helpers", None, "[]"], False),
        ("os.listdir", ["C:\\lib"], False),
    ],
)
def test_the_check(event, args, flagged):
    assert forbidden(event, args) is flagged


@pytest.mark.parametrize("command", ["log", "drive"])
@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_nothing_happens_before_the_safety_core_is_imported(tmp_path, entry, command):
    recorder = tmp_path / "recorder"
    recorder.mkdir()
    (recorder / "sitecustomize.py").write_text(RECORDER, encoding="utf-8")
    argv = {
        "log": ["log", "--data", str(tmp_path / "missing")],
        "drive": ["drive", "--seconds", "1", "--data", str(tmp_path / "data")],
    }[command]
    env = {**os.environ, "PYTHONPATH": str(recorder), "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([*ENTRY_POINTS[entry], *argv], capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr
    [line] = [text for text in done.stderr.splitlines() if text.startswith("LASTO-EVENTS ")]
    seen = json.loads(line.removeprefix("LASTO-EVENTS "))
    assert ["import", "lasto.cli"] in [[event, args[0]] for event, args in seen]  # recorded from before the CLI
    assert [entry for entry in seen if forbidden(*entry)] == []


def test_the_recorder_sees_what_happens_before_the_safety_core(tmp_path):
    """The control: a process that writes a file and loads a library, then imports the safety core."""
    recorder = tmp_path / "recorder"
    recorder.mkdir()
    (recorder / "sitecustomize.py").write_text(RECORDER, encoding="utf-8")
    early = tmp_path / "early.txt"
    script = f"import ctypes; open({str(early)!r}, 'w').close(); ctypes.WinDLL('kernel32'); import lasto.safety"
    env = {**os.environ, "PYTHONPATH": str(recorder), "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=120, check=True)
    [line] = [text for text in done.stderr.splitlines() if text.startswith("LASTO-EVENTS ")]
    flagged = [[event, args[0]] for event, args in json.loads(line.removeprefix("LASTO-EVENTS ")) if forbidden(event, args)]
    # import ctypes loads kernel32 itself, for GetLastError; lasto's first import of ctypes is inside the safety core's.
    assert ["open", str(early)] in flagged
    assert ["ctypes.dlopen", "kernel32"] in flagged


def test_the_console_script_is_installed():
    assert Path(ENTRY_POINTS["console script"][0]).is_file()
