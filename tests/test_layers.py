"""The layers from docs/architecture.md §14.4, and the serial guard in the CLI.

- lasto.services is hardware-free. The CLI and (from Phase 9) the GUI both call it, so nothing it
  reaches may be the safety core, the simulator, the hardware-facing operations, capture, a front
  end, or a hardware library.
- lasto.operations is the only code outside the safety core that opens the core's sessions.
- cli.py parses arguments and renders results. From lasto it calls services and operations, and
  imports the bare safety core package for its serial guard, nothing else.
- cli.main imports the safety core, which installs the serial guard, before it dispatches any command
  except gui. The GUI process must never import the safety core; it installs its own hook (§14.1).
"""

from __future__ import annotations

import ast
import subprocess
import sys

import pytest
from scan import sources

from lasto import cli

HARDWARE_LIBRARIES = {"serial", "ctypes", "_ctypes", "can"}
NOT_FOR_SERVICES = ("lasto.safety", "lasto.sim", "lasto.operations", "lasto.capture", "lasto.cli", "lasto.gui")


def _is(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def module_imports(tree: ast.AST) -> set[str]:
    """Every module a source imports, anywhere in it: `import a.b` gives a.b, `from a import b` gives a and a.b."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def reach(start: set[str]) -> set[str]:
    """The lasto modules the start modules import, directly or through each other, plus every other import."""
    seen: set[str] = set()
    todo = list(start)
    while todo:
        current = todo.pop()
        if current in seen:
            continue
        seen.add(current)
        if current in sources():
            todo.extend(module_imports(sources()[current]))
    return seen


def in_package(package: str) -> set[str]:
    return {name for name in sources() if _is(name, package)}


def test_the_layers_exist():
    assert {"lasto.services", "lasto.operations"} <= set(sources())


def test_services_are_hardware_free():
    reached = reach(in_package("lasto.services"))
    assert sorted(name for name in reached if any(_is(name, package) for package in NOT_FOR_SERVICES)) == []
    assert sorted(name for name in reached if name.split(".")[0] in HARDWARE_LIBRARIES) == []


def test_only_operations_open_safety_core_sessions():
    """The session openers live in lasto.safety.session; outside the core, only operations may reach them."""
    openers = {
        name
        for name, tree in sources().items()
        if not _is(name, "lasto.safety") and "lasto.safety.session" in module_imports(tree)
    }
    assert sorted(name for name in openers if not _is(name, "lasto.operations")) == []


def test_the_cli_calls_only_services_and_operations():
    lasto_imports = {name for name in module_imports(sources()["lasto.cli"]) if _is(name, "lasto")}
    allowed = {"lasto", "lasto.__version__", "lasto.safety"}
    extra = {name for name in lasto_imports - allowed if not (_is(name, "lasto.services") or _is(name, "lasto.operations"))}
    assert sorted(extra) == []


def test_only_gui_runs_without_the_safety_core():
    assert cli.SAFETY_CORE_FREE_COMMANDS == {"gui"}
    assert cli.COMMANDS["gui"][1] == 9


PROBE = """
import sys
import lasto.cli as cli


def loaded():
    return sorted(name for name in sys.modules if name == "lasto.safety" or name.startswith("lasto.safety."))


print("imported:", loaded())


def probe(args):
    print("dispatched:", loaded())
    return 0


cli._dispatch = probe
cli.main({argv!r})
"""


def run_command(argv: list[str]) -> dict[str, str]:
    done = subprocess.run([sys.executable, "-c", PROBE.format(argv=argv)], capture_output=True, text=True, check=True)
    return dict(line.split(": ", 1) for line in done.stdout.splitlines())


@pytest.mark.parametrize("argv", [["log"], ["drive"], ["map"]])
def test_the_serial_guard_is_installed_before_a_command_runs(argv):
    """Importing the CLI loads nothing of the safety core; dispatching any command but gui loads the guard first."""
    seen = run_command(argv)
    assert seen["imported"] == "[]"
    assert "'lasto.safety.serial_guard'" in seen["dispatched"]


def test_the_gui_command_never_loads_the_safety_core():
    seen = run_command(["gui"])
    assert seen == {"imported": "[]", "dispatched": "[]"}
