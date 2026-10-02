"""Guard v2, foreign functions (Phase 3, A4).

The scanner reads only src/lasto, so a dependency could still reach a port through ctypes, whose calls raise no
audit event. Looking a function up does (ctypes.dlsym), and so does loading a library (ctypes.dlopen), so the
guard allows only the libraries and functions lasto binds:

- PCANBasic.dll, by its path in the system folder: its CAN_ functions. Which of them lasto binds is checked by
  test_structure.py.
- kernel32: GetSystemDirectoryW (where PCANBasic.dll is), GetCurrentThreadId (the hotkey), and
  SetThreadExecutionState (keep_awake).
- user32: the hotkey's four functions.
- pyserial's kernel32 bindings, CreateFileW among them, only while stn_port imports pyserial, on that thread
  (`with PYSERIAL_IMPORT`). pyserial imported anywhere else fails.

Every test here that asks ctypes for something real checks it before anything could be called: a lookup the guard
let through would bind a function, never call it.
"""

from __future__ import annotations

import ast
import ctypes
import subprocess
import sys
import sysconfig
import threading
from pathlib import Path

import pytest
from helpers import events

from lasto.safety import hotkey, pcan_dll, serial_guard, stn_port
from lasto.safety.audit import REFUSALS
from lasto.safety.errors import SafetyViolation
from lasto.safety.serial_guard import FOREIGN_FUNCTIONS, PYSERIAL_FUNCTIONS, PYSERIAL_IMPORT, foreign_function_allowed

PCAN = pcan_dll.dll_path()


class Library:
    """Stands in for a loaded ctypes library: what the guard reads from one."""

    def __init__(self, name: object) -> None:
        self._name = name


# ---- the tables ----


def test_pcan_basic_allows_its_can_functions_and_nothing_else():
    """Which CAN_ functions lasto binds, and that only pcan_active binds CAN_Write, is test_structure.py's job: the
    guard is on the passive path, which names no write function."""
    for name in (*pcan_dll.READONLY_FUNCTIONS, "CAN_Write"):
        assert foreign_function_allowed("PCANBasic.dll", name, importing_pyserial=False), name
    for name in ("GetProcAddress", "CreateFileW", "can_read", "CAN_", "CAN_Read2", 3):
        assert not foreign_function_allowed("PCANBasic.dll", name, importing_pyserial=True), name
    assert not {name for library, name in FOREIGN_FUNCTIONS if library == "PCANBasic.dll"}


def _bound(module: object, holder: str) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute) and node.value.attr == holder
    }


def test_the_hotkey_functions_are_on_the_list():
    assert {("user32", name) for name in _bound(hotkey, "_user32")} <= FOREIGN_FUNCTIONS
    assert {("kernel32", name) for name in _bound(hotkey, "_kernel32")} <= FOREIGN_FUNCTIONS
    assert {name for library, name in FOREIGN_FUNCTIONS if library == "user32"} == _bound(hotkey, "_user32")


def test_the_pyserial_functions_are_the_ones_pyserial_looks_up():
    """Pinned to the locked pyserial: every kernel32 function serial/win32.py binds as it's imported, without the
    ANSI fallbacks it uses only when the wide functions are missing."""
    source = Path(sysconfig.get_paths()["purelib"]) / "serial" / "win32.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    looked_up = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Subscript)
        and ast.unparse(node.value) == "_stdcall_libraries['kernel32']"
    }
    assert looked_up - {"CreateEventA", "CreateFileA"} == PYSERIAL_FUNCTIONS
    assert not {name for _, name in FOREIGN_FUNCTIONS} & {"CreateFileW", "WriteFile", "ReadFile", "CreateFileA"}


@pytest.mark.parametrize(
    ("library", "name", "importing_pyserial", "allowed"),
    [
        ("PCANBasic.dll", "CAN_Read", False, True),
        ("kernel32", "CAN_Read", False, False),
        ("kernel32", "SetThreadExecutionState", False, True),
        ("kernel32", "CreateFileW", False, False),
        ("kernel32", "CreateFileW", True, True),
        ("kernel32", "WriteFile", True, True),
        ("kernel32", "GetProcAddress", True, False),
        ("kernel32", "LoadLibraryW", True, False),
        ("user32", "CreateFileW", True, False),
        ("user32", "RegisterHotKey", False, True),
        ("kernel32", 7, True, False),  # by ordinal
        (None, "CAN_Read", False, False),
    ],
)
def test_which_functions_ctypes_may_look_up(library, name, importing_pyserial, allowed):
    assert foreign_function_allowed(library, name, importing_pyserial=importing_pyserial) is allowed


# ---- the hook, called directly (coverage doesn't trace code while Python runs an audit hook) ----


@pytest.mark.parametrize("name", ["kernel32", "KERNEL32", "user32", PCAN])
def test_the_hook_lets_lasto_load_its_libraries(auditor, sink, name):
    REFUSALS.attach(auditor)
    serial_guard.guard_event("ctypes.dlopen", (name,))
    assert events(sink, "rejected") == []


@pytest.mark.parametrize(
    "name",
    [
        "advapi32",
        "kernel32.dll",  # not the name lasto loads it by
        "C:\\Users\\Public\\PCANBasic.dll",  # PCANBasic.dll, but not the system folder's
        "PCANBasic.dll",
        "setupapi",
    ],
)
def test_the_hook_refuses_any_other_library(auditor, sink, name):
    REFUSALS.attach(auditor)
    with pytest.raises(SafetyViolation) as refused:
        serial_guard.guard_event("ctypes.dlopen", (name,))
    assert refused.value.reason == "foreign_library_refused"
    [refusal] = events(sink, "rejected")
    assert refusal["request"] == f"ctypes.dlopen {name!r}"


def test_the_hook_decides_a_lookup(auditor, sink):
    REFUSALS.attach(auditor)
    serial_guard.guard_event("ctypes.dlsym", (Library(PCAN), "CAN_Read"))
    serial_guard.guard_event("ctypes.dlsym", (Library("user32"), "GetMessageW"))
    assert events(sink, "rejected") == []
    with pytest.raises(SafetyViolation) as refused:
        serial_guard.guard_event("ctypes.dlsym", (Library("kernel32"), "CreateFileW"))
    assert refused.value.reason == "foreign_function_refused"
    [refusal] = events(sink, "rejected")
    assert refusal["request"] == "ctypes.dlsym 'kernel32' 'CreateFileW'"


def test_the_hook_refuses_a_lookup_in_something_that_is_not_a_library(auditor, sink):
    REFUSALS.attach(auditor)
    with pytest.raises(SafetyViolation) as refused:
        serial_guard.guard_event("ctypes.dlsym", (object(), "CAN_Read"))
    assert refused.value.reason == "foreign_function_refused"


def test_the_hook_lets_pyserial_look_up_its_functions_only_while_stn_port_imports_it(auditor, sink):
    REFUSALS.attach(auditor)
    with PYSERIAL_IMPORT:
        serial_guard.guard_event("ctypes.dlsym", (Library("kernel32"), "CreateFileW"))
    assert events(sink, "rejected") == []
    with pytest.raises(SafetyViolation):
        serial_guard.guard_event("ctypes.dlsym", (Library("kernel32"), "CreateFileW"))


# ---- the real thing ----


def test_ctypes_refuses_a_library_lasto_does_not_load():
    with pytest.raises(SafetyViolation) as refused:
        ctypes.WinDLL("advapi32")
    assert refused.value.reason == "foreign_library_refused"


def test_ctypes_refuses_a_function_lasto_does_not_bind():
    kernel32 = ctypes.WinDLL("kernel32")
    for name in ("CreateFileW", "GetProcAddress", "LoadLibraryW"):
        with pytest.raises(SafetyViolation) as refused:
            getattr(kernel32, name)
        assert refused.value.reason == "foreign_function_refused"
    with pytest.raises(SafetyViolation):
        kernel32[1]  # by ordinal


def test_ctypes_binds_a_function_lasto_does():
    assert ctypes.WinDLL("kernel32").SetThreadExecutionState is not None


def test_the_pyserial_window_is_only_on_the_importing_thread():
    elsewhere: list[BaseException | None] = []

    def look_up() -> None:
        try:
            ctypes.WinDLL("kernel32").CreateFileW
            elsewhere.append(None)
        except SafetyViolation as exc:
            elsewhere.append(exc)

    with PYSERIAL_IMPORT:
        assert ctypes.WinDLL("kernel32").CreateFileW is not None
        thread = threading.Thread(target=look_up)
        thread.start()
        thread.join()
    [refused] = elsewhere
    assert isinstance(refused, SafetyViolation)
    with pytest.raises(SafetyViolation):
        ctypes.WinDLL("kernel32").CreateFileW  # and closed again


def test_stn_port_imports_pyserial_through_the_window():
    """In a test run, `import serial` finds the hardware firewall's stub, so this is the production path's shape only;
    the subprocess test below imports the real pyserial."""
    assert stn_port._pyserial() is sys.modules["serial"]


PRODUCTION = r"""
import sys
import lasto.safety
from lasto.safety import stn_port
from lasto.safety.errors import SafetyViolation

try:
    import serial
    print("imported")
except SafetyViolation as exc:
    print(exc.reason)
serial = stn_port._pyserial()
print("imported", serial.VERSION, serial.Serial.__module__)
"""


def test_outside_a_test_run_only_stn_port_imports_pyserial():
    """Imports the real pyserial, which binds its kernel32 functions and opens nothing."""
    done = subprocess.run([sys.executable, "-c", PRODUCTION], capture_output=True, text=True, check=True)
    assert done.stdout.splitlines() == ["foreign_function_refused", "imported 3.5 serial.serialwin32"]
