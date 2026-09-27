"""The Claude Code hardware guard hook (.claude/hooks/hardware_guard.py)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "hardware_guard.py"
_spec = importlib.util.spec_from_file_location("hardware_guard", HOOK)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

DENY = [
    ("Bash", "lasto drive --live --channel PCAN_USBBUS1"),
    ("Bash", "lasto drive --live"),
    ("Bash", "lasto drive --liv"),
    ("Bash", "lasto drive --li"),
    ("Bash", "lasto drive --l"),
    ("Bash", "lasto drive --live=1"),
    ("Bash", "lasto drive --live-capture"),
    ("PowerShell", "lasto drive --live"),
    ("Bash", "python -m lasto identify --channel pcan_usbbus2"),
    ("Bash", "lasto crank --port COM5"),
    ("Bash", "mode com12"),
    ("PowerShell", "[System.IO.Ports.SerialPort]::new('\\\\.\\COM7')"),
    ("Bash", "cat /dev/ttyUSB0"),
    ("Bash", "screen /dev/cu.OBDLink"),
    ("Bash", 'python -c "from can.interfaces.pcan.basic import PCANBasic; PCANBasic()"'),
    ("Bash", "python -c \"import can; can.Bus(interface='pcan')\""),
    ("Bash", 'python -c "import can; can.Bus(bustype = \\"pcan\\")"'),
    ("Bash", "python -c \"import serial; serial.Serial('x')\""),
    ("Bash", 'python -c "import can; can.Bus()"'),
    ("Bash", "python -c \"import can; can.interface.Bus(channel='x')\""),
    ("Bash", "CAN_INTERFACE=pcan python x.py"),
    ("Bash", "python -c \"cfg = {'interface': 'pcan'}\""),
    ("Monitor", "lasto view --live"),
    ("mcp__terminal__open_terminal_tab", "lasto drive --live"),
]

ASK = [
    ("Bash", {"command": "sed -i 's/a/b/' src/lasto/safety/gate.py"}),
    ("PowerShell", {"command": "Set-Content src\\lasto\\safety\\policy.py 'x'"}),
    ("Agent", {"prompt": "Refactor src/lasto/safety/gate.py", "subagent_type": "general-purpose"}),
    ("Agent", {"prompt": "Review the safety core allowlists"}),
    ("Task", {"prompt": "Run lasto drive --live and report"}),
    ("Workflow", {"script": "agent('open PCAN_USBBUS1 and read frames')"}),
    ("Workflow", {"script": "agent('check lasto.safety.policy')"}),
]

ALLOW = [
    ("Bash", {"command": "python -m pytest"}),
    ("Bash", {"command": "python -m pytest tests/safety -q"}),
    ("Bash", {"command": "lasto drive"}),
    ("Bash", {"command": "lasto drive --profile grades --limit 10"}),
    ("Bash", {"command": "git commit -m 'Add passive capture for the PCAN reader'"}),
    ("Bash", {"command": "git log --oneline"}),
    ("Bash", {"command": "ls --long-listing"}),
    ("Bash", {"command": "echo computer community"}),
    ("Bash", {"command": "pip install python-can pyserial"}),
    ("Edit", {"file_path": "src/lasto/cli.py", "new_string": "--live PCAN_USBBUS1 COM3"}),
    ("Write", {"file_path": "tests/test_cli.py", "content": "args = ['--live']"}),
    ("Read", {"file_path": "src/lasto/safety/gate.py"}),
    ("Agent", {"prompt": "Summarize the matplotlib chart options for the report"}),
    ("Workflow", {"script": "agent('research zstandard frame format')"}),
    ("Bash", {"command": "echo ok", "nested": {"list": ["fine", 3, None]}}),
]


@pytest.mark.parametrize(("tool", "command"), DENY)
def test_denied(tool, command):
    decision, reason = guard.decide({"tool_name": tool, "tool_input": {"command": command}})
    assert decision == "deny"
    assert "user" in reason


@pytest.mark.parametrize(("tool", "tool_input"), ASK)
def test_asks(tool, tool_input):
    assert guard.decide({"tool_name": tool, "tool_input": tool_input})[0] == "ask"


@pytest.mark.parametrize(("tool", "tool_input"), ALLOW)
def test_allowed(tool, tool_input):
    assert guard.decide({"tool_name": tool, "tool_input": tool_input}) is None


@pytest.mark.parametrize("event", [{"tool_input": {"command": "ls"}}, {"tool_name": "Bash", "tool_input": "ls"}])
def test_malformed_events_raise(event):
    with pytest.raises(ValueError):
        guard.decide(event)


def run_hook(payload: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([sys.executable, str(HOOK)], input=payload, capture_output=True, timeout=30)


def test_hook_output_format():
    result = run_hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": "lasto drive --live"}}).encode())
    assert result.returncode == 0
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert (output["hookEventName"], output["permissionDecision"]) == ("PreToolUse", "deny")
    quiet = run_hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}}).encode())
    assert (quiet.returncode, quiet.stdout) == (0, b"")


@pytest.mark.parametrize("payload", [b"", b"{not json", b"[]", b'{"tool_input": {}}'])
def test_hook_fails_closed(payload):
    result = run_hook(payload)
    assert result.returncode == 2
    assert b"blocking" in result.stderr
