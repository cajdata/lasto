"""Guard v2, child processes and Bluetooth (Phase 3, A3).

- A child process runs without this process's audit hook, so it could open anything. Outside a test run, the
  guard refuses every way Python starts one: subprocess.Popen, _winapi.CreateProcess, os.system, os.startfile,
  os.spawn* and os.exec*. A test run may, since the tests start subprocesses; test_structure.py holds that
  nothing in src/ starts one, so the tests' allowance can't hide one.
- A Bluetooth socket (AF_BLUETOOTH, 32) could reach the MX+ with no COM port at all. It's refused everywhere,
  before the socket exists.
"""

from __future__ import annotations

import socket
import subprocess
import sys

import pytest
from helpers import events

from lasto.safety import serial_guard
from lasto.safety.audit import REFUSALS
from lasto.safety.errors import SafetyViolation

BLUETOOTH = (socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)


def test_a_test_run_may_start_a_child_process():
    subprocess.run([sys.executable, "-c", "pass"], check=True)


def test_the_hook_lets_a_test_run_start_a_child_process(auditor, sink):
    """Called directly: coverage doesn't trace code while Python runs an audit hook."""
    REFUSALS.attach(auditor)
    serial_guard.guard_event("os.system", ("exit 0",))
    assert events(sink, "rejected") == []


@pytest.mark.parametrize(
    ("event", "args"),
    [
        ("subprocess.Popen", ("python", ["python", "-c", "pass"], None, None)),
        ("_winapi.CreateProcess", (None, "python -c pass", None)),
        ("os.system", ("exit 0",)),
        ("os.startfile", ("notes.txt", "open")),
        ("os.startfile/2", ("notes.txt", "open", "", None, 1)),
        ("os.spawn", (0, "python", ["python"], None)),
        ("os.exec", ("python", ["python"], None)),
    ],
)
def test_outside_a_test_run_each_way_to_start_a_child_process_is_refused(auditor, sink, event, args):
    REFUSALS.attach(auditor)
    serial_guard.check_child_process(event, args, test_run=True)
    assert events(sink, "rejected") == []
    with pytest.raises(SafetyViolation) as refused:
        serial_guard.check_child_process(event, args, test_run=False)
    assert refused.value.reason == "child_process_refused"
    [refusal] = events(sink, "rejected")
    assert refusal["request"].startswith(event + " ")


PRODUCTION = r"""
import _winapi, os, subprocess, sys
import lasto.safety
from lasto.safety.errors import SafetyViolation

python = sys.executable

def attempt(name, start):
    try:
        start()
        print(name, "started")
    except SafetyViolation as exc:
        print(name, exc.reason)

attempt("Popen", lambda: subprocess.run([python, "-c", "pass"], check=True))
attempt("CreateProcess", lambda: _winapi.CreateProcess(None, f'"{python}" -c pass', None, None, False, 0, None, None, subprocess.STARTUPINFO()))
attempt("system", lambda: os.system("exit 0"))
# os.startfile raises this before it does anything. A real call the guard let through would open a window.
attempt("startfile", lambda: sys.audit("os.startfile", "notes.txt", "open"))
attempt("spawn", lambda: os.spawnv(os.P_WAIT, python, [python, "-c", "pass"]))
attempt("exec", lambda: os.execv(python, [python, "-c", "print('exec started')"]))  # last: it would replace this process
"""


def test_outside_a_test_run_no_child_process_starts():
    done = subprocess.run([sys.executable, "-c", PRODUCTION], capture_output=True, text=True, check=True)
    assert done.stdout.splitlines() == [
        f"{name} child_process_refused" for name in ("Popen", "CreateProcess", "system", "startfile", "spawn", "exec")
    ]


def test_a_bluetooth_socket_is_refused_before_it_exists(auditor, sink):
    REFUSALS.attach(auditor)
    # The hook first, through the event alone: if it let Bluetooth through, the test stops here, with no socket.
    with pytest.raises(SafetyViolation) as refused:
        sys.audit("socket.__new__", None, *BLUETOOTH)
    assert refused.value.reason == "bluetooth_socket_refused"
    with pytest.raises(SafetyViolation):
        socket.socket(*BLUETOOTH)
    assert [record["request"] for record in events(sink, "rejected")] == ["socket.__new__ family 32"] * 2


def test_the_hook_decides_a_socket(auditor, sink):
    """Called directly: coverage doesn't trace code while Python runs an audit hook."""
    REFUSALS.attach(auditor)
    serial_guard.guard_event("socket.__new__", (None, socket.AF_INET, socket.SOCK_STREAM, 0))
    assert events(sink, "rejected") == []
    with pytest.raises(SafetyViolation) as refused:
        serial_guard.guard_event("socket.__new__", (None, *BLUETOOTH))
    assert refused.value.reason == "bluetooth_socket_refused"
