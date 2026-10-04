"""Structural rules, checked by reading the source.

- Only safety/pcan_active.py binds or calls CAN_Write (the simulator's fake DLL may list it), and
  only its Writer holds it. Only the gate calls the Writer: the session hands it straight to the gate,
  the gate calls it once in _transmit, and no other safety module names a writer.
- The passive path imports nothing that can transmit and names no write function.
- Only safety/stn_port.py opens a serial port, and no literal in src/ names a serial device path.
- Only the hardware bindings use ctypes, and operations/keep_awake.py, which may bind one kernel32
  function, SetThreadExecutionState, and nothing else (review finding L10).
- Only the session module imports the transmit binding and the gate.
- Every argument parser disables option abbreviation, so nothing shorter than --live can enable it.
- No eval/exec anywhere, and nothing in src/ starts a child process (guard v2 refuses one at runtime, except
  in a test run) or creates a subinterpreter.
- src/ copies a file only through open(): no shutil copies, no _winapi, none of pathlib's copy and move
  methods (Step A review M1).
- Only storage/database.py opens SQLite connections, each with an authorizer that refuses ATTACH, and no SQL
  literal in src/ attaches another file (Step A review L14).
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest
from scan import bindings

import lasto

SRC = Path(lasto.__file__).parent


@cache
def sources() -> dict[str, ast.Module]:
    found = {}
    for path in sorted(SRC.rglob("*.py")):
        parts = list(path.relative_to(SRC.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        found[".".join(parts)] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return found


def docstrings(tree: ast.Module) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def mentions(tree: ast.Module, text: str) -> list[str]:
    """Code (not docstrings or comments) that names `text`: identifiers, attributes, or string literals."""
    skip = docstrings(tree)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and text in node.id:
            hits.append("name")
        elif isinstance(node, ast.Attribute) and text in node.attr:
            hits.append("attribute")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and text in node.value and id(node) not in skip:
            hits.append("string")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and text in node.name:
            hits.append("def")
        elif isinstance(node, ast.alias) and text in node.name:
            hits.append("import")
    return hits


def imports(tree: ast.Module) -> set[str]:
    names = set()
    known = sources()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names if f"{node.module}.{alias.name}" in known)
    return names


def importers(module: str) -> set[str]:
    return {name for name, tree in sources().items() if module in imports(tree)}


def closure(module: str) -> set[str]:
    seen, todo = set(), [module]
    while todo:
        current = todo.pop()
        if current in seen or current not in sources():
            continue
        seen.add(current)
        todo.extend(name for name in imports(sources()[current]) if name.startswith("lasto"))
    return seen


def _in_the_core(name: str) -> bool:
    return name == "lasto.safety" or name.startswith("lasto.safety.")


def lasto_imports_outside_the_core(module: str, tree: ast.Module) -> list[str]:
    """For a safety module, every lasto module it imports that isn't part of the safety core.

    The safety core is self-contained: settings, storage, services, and the rest of lasto never feed it,
    so nothing a user can edit, and nothing a future phase adds outside the core, can change what it
    allows (docs/architecture.md §14.8). Imports inside functions and TYPE_CHECKING blocks count too.
    """
    if not _in_the_core(module):
        return []
    return sorted(name for name in imports(tree) if (name == "lasto" or name.startswith("lasto.")) and not _in_the_core(name))


@pytest.mark.parametrize(
    ("module", "snippet", "flagged"),
    [
        ("lasto.safety.probe", "from lasto import config", True),
        ("lasto.safety.probe", "import lasto.storage.db", True),
        ("lasto.safety.probe", "from lasto.records import Frame", True),
        ("lasto.safety.probe", "def limits():\n    from lasto.services import settings", True),
        ("lasto.safety.probe", "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from lasto.storage import db", True),
        ("lasto.safety.probe", "import lasto", True),
        ("lasto.safety.probe", "from lasto.safety import audit\nimport lasto.safety.policy\nimport json", False),
        ("lasto.storage.db", "from lasto import config", False),  # outside the core, other rules apply
    ],
)
def test_the_self_contained_core_check(module, snippet, flagged):
    assert bool(lasto_imports_outside_the_core(module, ast.parse(snippet))) is flagged


def test_the_safety_core_imports_nothing_from_lasto_outside_itself():
    found = {name: outside for name, tree in sources().items() if (outside := lasto_imports_outside_the_core(name, tree))}
    assert sum(_in_the_core(name) for name in sources()) >= 25
    assert found == {}


def test_the_scan_sees_the_code():
    assert {"lasto.safety.gate", "lasto.safety.pcan_active", "lasto.safety.pcan_passive", "lasto.cli"} <= set(sources())


def test_only_pcan_active_binds_can_write():
    for name, tree in sources().items():
        hits = mentions(tree, "CAN_Write")
        if name == "lasto.safety.pcan_active":
            # One lookup by attribute; the error message names it too.
            assert sorted(hits) == ["attribute", "string"], "pcan_active looks up CAN_Write once, by attribute"
        elif name == "lasto.sim.fake_pcan":
            assert set(hits) == {"string"}, "the fake DLL may only list CAN_Write in its function table"
        else:
            assert hits == [], f"{name} names CAN_Write"


def test_only_pcan_active_touches_the_transmit_call():
    for name, tree in sources().items():
        if name != "lasto.safety.pcan_active":
            assert mentions(tree, "Writer") == [], name
            assert mentions(tree, "_can_write") == [] or name == "lasto.sim.fake_pcan", name


def test_passive_path_imports_nothing_that_can_transmit():
    passive = closure("lasto.safety.pcan_passive")
    assert "lasto.safety.pcan_dll" in passive
    forbidden = {"lasto.safety.pcan_active", "lasto.safety.gate", "lasto.safety.session", "lasto.safety.stn_port", "lasto.safety.hotkey"}
    assert not passive & forbidden
    for name in passive:
        tree = sources()[name]
        assert mentions(tree, "CAN_Write") == [], name
        if name == "lasto.safety.audit":
            continue  # writes the audit log file, never the channel
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and "write" in n.func.attr.lower()]
        assert calls == [], f"{name} calls a write method"



# Raises in the safety core that aren't refusals, by (module, function, what is raised), each with why.
# Anything else raised outside refuse() fails the suite. Changing this list needs the owner's approval.
NOT_REFUSALS = {
    ("lasto.safety.audit", "_deliver", "failures[0]"): (
        "re-raises an audit log's own failure, after every attached audit log has had the record (finding #8)"
    ),
    ("lasto.safety.killswitch", "trip", "errors[0]"): (
        "re-raises a kill listener's failure, after the latch is set, every listener has run, and the trip is audited"
    ),
    ("lasto.safety.isotp", "parse", "IsoTpError"): (
        "an ISO-TP parse error: the policy refuses (audited) outgoing bytes that raise one, and the gate trips "
        "the kill switch on a received one"
    ),
}


def refusals_outside_refuse(module: str, tree: ast.Module) -> list[str]:
    """Every raise in the safety core that isn't audit.refuse() raising a refusal it has recorded.

    Allowed: refuse() itself, a bare raise (re-raising what was caught), and NOT_REFUSALS. Anything else,
    of any type, however it is built, fails, and so does an assert, which raises and vanishes under -O.
    """
    found = []
    if not module.startswith("lasto.safety"):
        return found
    _, functions = _context(tree)
    for node in ast.walk(tree):
        where = functions.get(id(node), "")
        if isinstance(node, ast.Assert):
            found.append(f"line {node.lineno}: assert in {where or 'the module'}")
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        if module == "lasto.safety.audit" and where == "refuse":
            continue  # the one place a refusal is raised, once it is recorded
        exc = node.exc
        raised = _identifier(exc.func) if isinstance(exc, ast.Call) else ast.unparse(exc)
        if (module, where, raised) not in NOT_REFUSALS:
            found.append(f"line {node.lineno}: raise {ast.unparse(exc)} in {where or 'the module'}")
    return found


@pytest.mark.parametrize(
    ("module", "snippet", "flagged"),
    [
        ("lasto.safety.probe", "raise SafetyViolation('reason')", True),
        ("lasto.safety.probe", "raise errors.InterfaceError('driver')", True),
        ("lasto.safety.probe", "raise RuntimeError('any type at all')", True),
        ("lasto.safety.probe", "error = SafetyViolation('reason')\nraise error", True),
        ("lasto.safety.probe", "raise KeyError(key) from None", True),
        ("lasto.safety.probe", "raise SafetyViolation", True),
        ("lasto.safety.probe", "assert allowed, 'refused'", True),  # an assert is a raise, and vanishes under -O
        ("lasto.safety.probe", "try:\n    check()\nexcept OSError:\n    raise", False),  # re-raises what was caught
        ("lasto.safety.audit", "def refuse(error):\n    record(error)\n    raise error", False),
        # The raises that aren't refusals, each only where it belongs.
        ("lasto.safety.isotp", "def parse(data):\n    raise IsoTpError('isotp_malformed', 'no protocol byte')", False),
        ("lasto.safety.gate", "def check(data):\n    raise IsoTpError('isotp_malformed', 'no protocol byte')", True),
        ("lasto.safety.audit", "def _deliver(self):\n    raise failures[0]", False),
        ("lasto.safety.killswitch", "def trip(self):\n    raise errors[0]", False),
        ("lasto.safety.policy", "def check():\n    raise failures[0]", True),
    ],
)
def test_the_refusal_check(module, snippet, flagged):
    """Review finding: the old check only caught raise X(...) for seven named refusal types."""
    assert bool(refusals_outside_refuse(module, ast.parse(snippet))) is flagged


def test_every_refusal_in_the_safety_core_goes_through_refuse():
    """Rule 11: a refusal is raised only by audit.refuse(), which records it first."""
    found = {name: problems for name, tree in sources().items() if (problems := refusals_outside_refuse(name, tree))}
    assert found == {}
    raising_sites = 0
    for name, tree in sources().items():
        if not name.startswith("lasto.safety"):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "refuse":
                raising_sites += 1
    refuse_def = next(
        n for n in ast.walk(sources()["lasto.safety.audit"]) if isinstance(n, ast.FunctionDef) and n.name == "refuse"
    )
    assert any(isinstance(n, ast.Raise) for n in ast.walk(refuse_def))
    assert raising_sites >= 30


def test_importing_the_passive_path_does_not_load_the_transmit_binding():
    code = "import sys, lasto.safety.pcan_passive; print(','.join(sorted(m for m in sys.modules if m.startswith('lasto'))))"
    loaded = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip().split(",")
    assert "lasto.safety.pcan_passive" in loaded
    assert "lasto.safety.pcan_active" not in loaded
    assert "lasto.safety.gate" not in loaded


def test_only_the_session_imports_the_transmit_binding_and_the_gate():
    assert importers("lasto.safety.pcan_active") == {"lasto.safety.session"}
    assert importers("lasto.safety.gate") == {"lasto.safety.session"}
    assert all(name.startswith("lasto.safety.") for name in importers("lasto.safety.pcan_dll"))


def attribute_calls(tree: ast.Module, attr: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr]


WRITE_FUNCTION = re.compile(r"(?i)writer|can_write")


def _identifier(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.arg):
        return node.arg
    return ""


def _context(tree: ast.AST) -> tuple[dict[int, ast.AST], dict[int, str]]:
    """Each node's parent, and the name of the function it sits in ("" at module level)."""
    parents: dict[int, ast.AST] = {}
    functions: dict[int, str] = {}

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else function
            parents[id(child)], functions[id(child)] = node, inner
            visit(child, inner)

    visit(tree, "")
    return parents, functions


def _handed_to_the_gate(tree: ast.AST, parents: dict[int, ast.AST]) -> tuple[set[int], list[str]]:
    """Follow the write function from open_active: it may only be unpacked and passed to Gate(...).

    Returns the nodes that do exactly that, and a problem for anything else."""
    fine: set[int] = set()
    found: list[str] = []
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and _identifier(call.func) == "open_active"):
            continue
        assign = parents.get(id(call))
        target = assign.targets[0] if isinstance(assign, ast.Assign) and len(assign.targets) == 1 else None
        if not (isinstance(target, ast.Tuple) and len(target.elts) == 2 and all(isinstance(e, ast.Name) for e in target.elts)):
            found.append(f"line {call.lineno}: open_active's result isn't unpacked into (channel, writer)")
            continue
        writer = target.elts[1]
        fine.add(id(writer))
        for use in ast.walk(tree):
            if isinstance(use, ast.Name) and use.id == writer.id and use is not writer:  # type: ignore[union-attr]
                gate = parents.get(id(use))
                if isinstance(gate, ast.Call) and _identifier(gate.func) == "Gate" and any(arg is use for arg in gate.args):
                    fine.add(id(use))
                else:
                    found.append(f"line {use.lineno}: uses the write function other than handing it to the gate")
    return fine, found


def _only_in(tree: ast.AST, attribute: str, function: str, parents: dict[int, ast.AST], functions: dict[int, str]) -> list[str]:
    """`attribute` is called exactly once, in `function`; otherwise it is only assigned, or closed in close()."""
    found, calls = [], 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr == attribute) or isinstance(node.ctx, ast.Store):
            continue
        parent, where = parents[id(node)], functions[id(node)]
        if isinstance(parent, ast.Call) and parent.func is node and where == function:
            calls += 1
        elif not (isinstance(parent, ast.Attribute) and parent.attr == "close" and where == "close"):
            found.append(f"line {node.lineno}: {attribute} used in {where or 'the module'}")
    if calls != 1:
        found.append(f"{attribute} is called {calls} times in {function}, not once")
    return found


def write_path_problems(module: str, tree: ast.Module) -> list[str]:
    """Where safety core code writes a frame, or handles the write function, anywhere but the gate's one call.

    The session hands the write function from open_active to the gate and does nothing else with it; the
    gate calls it once, in _transmit; only the Writer's _write calls CAN_Write; and no other safety module
    names a writer at all, so a bare writer(...) call can't hide behind a local name (review finding).
    """
    found = []
    if not module.startswith("lasto.safety"):
        return found
    # audit writes its log file; stn_port writes to the adapter behind its own command allowlist.
    if attribute_calls(tree, "write") and module not in {"lasto.safety.audit", "lasto.safety.stn_port"}:
        found.append("calls a write method")
    parents, functions = _context(tree)
    if module == "lasto.safety.gate":
        return found + _only_in(tree, "_writer", "_transmit", parents, functions)
    if module == "lasto.safety.pcan_active":
        return found + _only_in(tree, "_can_write", "_write", parents, functions)
    fine, problems = _handed_to_the_gate(tree, parents)
    found += problems
    named = sorted(
        {
            f"line {getattr(node, 'lineno', '?')}: {_identifier(node)}"
            for node in ast.walk(tree)
            if WRITE_FUNCTION.search(_identifier(node)) and id(node) not in fine
        }
    )
    return found + [f"names a write function ({where})" for where in named]


@pytest.mark.parametrize(
    ("module", "snippet", "flagged"),
    [
        # The session gets the write function from open_active and hands it to the gate, and nothing else.
        ("lasto.safety.session", "channel, writer = open_active(name)\ngate = Gate(writer, auditor)", False),
        ("lasto.safety.session", "channel, writer = open_active(name)\nGate(writer)\nwriter(0x7E0, data, purpose='p', kind='request')", True),
        ("lasto.safety.session", "channel, writer = open_active(name)\nsend = writer\nGate(writer)", True),
        ("lasto.safety.session", "pair = open_active(name)\nGate(pair[1])", True),
        ("lasto.safety.reader", "def poll(self):\n    self._writer(0x7E0, data)", True),
        ("lasto.safety.reader", "def poll(self, writer):\n    writer(0x7E0, data)", True),
        # The gate calls it once, in _transmit, and closes it in close.
        ("lasto.safety.gate", "class Gate:\n    def _transmit(self):\n        self._writer(1)\n    def close(self):\n        self._writer.close()", False),
        ("lasto.safety.gate", "class Gate:\n    def _transmit(self):\n        self._writer(1)\n    def poll(self):\n        self._writer(2)", True),
        ("lasto.safety.gate", "class Gate:\n    def submit(self):\n        send = self._writer", True),
        # Only the Writer's _write calls CAN_Write.
        ("lasto.safety.pcan_active", "class Writer:\n    def _write(self):\n        self._can_write(1)\n    def other(self):\n        self._can_write(2)", True),
    ],
)
def test_the_write_path_check(module, snippet, flagged):
    """Review finding: a bare writer(...) call in session.py passed the old attribute-name check."""
    assert bool(write_path_problems(module, ast.parse(snippet))) is flagged


def test_frames_are_written_only_by_the_gate_through_the_writer():
    found = {name: problems for name, tree in sources().items() if (problems := write_path_problems(name, tree))}
    assert found == {}
    assert len(attribute_calls(sources()["lasto.safety.gate"], "_writer")) == 1
    assert len(attribute_calls(sources()["lasto.safety.pcan_active"], "_can_write")) == 1


def test_only_stn_port_opens_a_serial_port():
    for name, tree in sources().items():
        uses_serial = any(i == "serial" or i.startswith("serial.") for i in imports(tree))
        assert uses_serial == (name == "lasto.safety.stn_port"), name


def names_a_serial_device(value: str | bytes) -> bool:
    """A literal naming a serial port (COM5, AUX), or a Windows device path that could reach one (\\\\.\\ and the like)."""
    text = value.decode("latin-1") if isinstance(value, bytes) else value
    path = text.replace("/", "\\")
    if path.startswith(("\\\\.\\", "\\\\?\\", "\\??\\")):
        return True
    return any(
        re.fullmatch(r"(?i)com[0-9¹²³]+|aux|globalroot", part.split(".")[0].split(":")[0].rstrip())
        for part in path.split("\\")
    )


@pytest.mark.parametrize(
    ("literal", "flagged"),
    [
        ("COM5", True),
        (b"COM3", True),
        ("com12:", True),
        ("AUX", True),
        (r"C:\temp\COM1.txt", True),
        (r"\\.\COM5", True),
        ("//./COM5", True),
        (r"\\?\GLOBALROOT\Device\Serial0", True),
        (r"\??\COM5", True),
        ("OBDLink COM port such as COM5 (only with --live)", False),
        ("--port must look like COM5, not ", False),
        (r"COM[1-9][0-9]{0,2}", False),
        ("company.txt", False),
        ("PCAN_USBBUS1", False),
    ],
)
def test_the_serial_device_check(literal, flagged):
    assert names_a_serial_device(literal) is flagged


def test_no_code_names_a_serial_device_path():
    """Finding P2: open("COM5") or os.open(r"\\\\.\\COM5") would reach the adapter without the STN link. stn_port
    takes its port name from the caller and checks it, so no literal anywhere in src/ names one (docstrings aside).
    The safety core's audit hook (lasto.safety.serial_guard) refuses such an open at runtime, whatever the path."""
    found = []
    for name, tree in sources().items():
        skip = docstrings(tree)
        found += [
            f"{name}: {node.value!r}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str | bytes)
            and id(node) not in skip
            and names_a_serial_device(node.value)
        ]
    assert found == []


def test_ctypes_only_in_the_hardware_bindings():
    allowed = {
        "lasto.safety.pcan_constants",
        "lasto.safety.pcan_dll",
        "lasto.safety.pcan_active",
        "lasto.safety.hotkey",
        "lasto.operations.keep_awake",  # kernel32 SetThreadExecutionState only; loads no hardware driver
        "lasto.sim.pytest_plugin",  # the test firewall
    }
    users = {name for name, tree in sources().items() if any(i == "ctypes" or i.startswith("ctypes.") for i in imports(tree))}
    assert users <= allowed


KEEP_AWAKE = "lasto.operations.keep_awake"
KEEP_AWAKE_VALID = """
from __future__ import annotations
import ctypes
def bind():
    function = ctypes.WinDLL("kernel32").SetThreadExecutionState
    function.argtypes = [ctypes.c_uint32]
    function.restype = ctypes.c_uint32
    return function
"""


KEEP_AWAKE_IMPORTS = {"__future__", "ctypes", "threading", "collections.abc", "types"}
KEEP_AWAKE_CTYPES = {"WinDLL", "c_uint32"}  # the library loader, and the argument and result type


def keep_awake_problems(tree: ast.Module) -> list[str]:
    """Review finding L10: what would take keep_awake.py past its one kernel32 function.

    - It imports only what it needs, so no other route to Windows (msvcrt, _winapi, os, the PCAN
      bindings) comes in, and it imports ctypes under its own name only.
    - From ctypes it takes only WinDLL and c_uint32, and never hands ctypes on as a value.
    - It calls WinDLL exactly once, as WinDLL("kernel32"), and takes SetThreadExecutionState straight off
      that call, so no library object is kept to take anything else from.
    """
    problems = []
    parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    loads = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in KEEP_AWAKE_IMPORTS:
                    problems.append(f"imports {alias.name}")
                elif alias.asname is not None:
                    problems.append(f"imports {alias.name} as {alias.asname}")
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module not in KEEP_AWAKE_IMPORTS or node.module == "ctypes":
                problems.append(f"imports from {node.module}")
        elif isinstance(node, ast.Name) and node.id == "ctypes":
            parent = parents.get(id(node))
            if not (isinstance(parent, ast.Attribute) and parent.value is node):
                problems.append("uses ctypes as a value")
            elif parent.attr not in KEEP_AWAKE_CTYPES:
                problems.append(f"takes ctypes.{parent.attr}")
            elif parent.attr == "WinDLL":
                loads.append(parent)
    if len(loads) != 1:
        problems.append(f"calls WinDLL {len(loads)} times")
    for load in loads:
        call = parents.get(id(load))
        if not (isinstance(call, ast.Call) and call.func is load):
            problems.append("takes WinDLL without calling it")
            continue
        argument = call.args[0] if len(call.args) == 1 else None
        if call.keywords or not (isinstance(argument, ast.Constant) and argument.value == "kernel32"):
            problems.append("loads something other than WinDLL('kernel32')")
        taken = parents.get(id(call))
        if not (isinstance(taken, ast.Attribute) and taken.value is call and taken.attr == "SetThreadExecutionState"):
            problems.append("takes something other than SetThreadExecutionState from kernel32")
    return problems


def test_keep_awake_binds_one_kernel32_function_and_nothing_else():
    """Review finding L10: keep_awake.py's ctypes exemption covers SetThreadExecutionState and nothing more."""
    assert keep_awake_problems(sources()[KEEP_AWAKE]) == []
    assert keep_awake_problems(ast.parse(KEEP_AWAKE_VALID)) == []


@pytest.mark.parametrize(
    "change",
    [
        # Another kernel32 function: CreateFileW and WriteFile would reach a COM port the serial guard can't see.
        "\ndef more():\n    return ctypes.WinDLL('kernel32').CreateFileW",
        "\ndef more():\n    return ctypes.WinDLL('kernel32').SetThreadExecutionState",  # a second binding
        "\ndef more():\n    library = ctypes.WinDLL('kernel32')\n    return library.WriteFile",  # a library kept to take more from
        "\ndef more():\n    return ctypes.WinDLL('user32').SetThreadExecutionState",  # another library
        "\ndef more(name):\n    return ctypes.WinDLL(name).SetThreadExecutionState",  # a library named at run time
        "\ndef more():\n    return ctypes.WinDLL('kernel32', use_last_error=True).SetThreadExecutionState",
        "\ndef more():\n    return getattr(ctypes.WinDLL('kernel32'), 'Set' + 'ThreadExecutionState')",
        "\ndef more():\n    return ctypes.CDLL('kernel32')",  # any other part of ctypes
        "\ndef more():\n    return ctypes.windll.kernel32.SetThreadExecutionState",
        "\ndef more():\n    return ctypes.cast",
        "\nlibrary = ctypes",  # ctypes handed on as a value
        "\nimport ctypes as c",  # under another name, which the attribute check wouldn't follow
        "\nfrom ctypes import WinDLL",
        "\nimport msvcrt",  # any other route to Windows
        "\nimport _winapi",
        "\nimport os",
        "\nfrom lasto.safety import pcan_dll",
    ],
)
def test_the_keep_awake_check_catches(change):
    assert keep_awake_problems(ast.parse(KEEP_AWAKE_VALID + change)) != []


_SQL_OPENS_ANOTHER_FILE = re.compile(r"(?i)\battach\s+(database\s+)?['\"?:]|\bvacuum\s+into\b")


def sql_opens_another_file(value: str | bytes) -> bool:
    """SQL that makes SQLite open a second file itself: ATTACH DATABASE, or VACUUM INTO."""
    text = value.decode("latin-1") if isinstance(value, bytes) else value
    return _SQL_OPENS_ANOTHER_FILE.search(text) is not None


@pytest.mark.parametrize(
    ("literal", "flagged"),
    [
        ("ATTACH DATABASE 'x.sqlite' AS x", True),
        ("attach 'x.sqlite' as x", True),
        ("ATTACH ? AS other", True),
        ("ATTACH :name AS other", True),
        ("VACUUM INTO 'copy.sqlite'", True),
        ("vacuum  into ?", True),
        ("REFUSALS.attach(auditor)", False),
        ("attach the audit log first", False),
        ("VACUUM", False),
    ],
)
def test_the_sql_check(literal, flagged):
    assert sql_opens_another_file(literal) is flagged


def test_no_sql_in_src_opens_another_database_file():
    """Guard v2 (Phase 3): SQLite opens an ATTACHed or VACUUM INTO file itself, where the safety core's audit hook
    can't see it. lasto's own connections refuse both at runtime (lasto.storage.database); no literal asks."""
    found = []
    for name, tree in sources().items():
        skip = docstrings(tree)
        found += [
            f"{name}: {node.value!r}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str | bytes)
            and id(node) not in skip
            and sql_opens_another_file(node.value)
        ]
    assert found == []


def star_imports(tree: ast.Module) -> list[str]:
    return [
        f"from {'.' * node.level}{node.module or ''} import *"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
    ]


@pytest.mark.parametrize(
    ("snippet", "flagged"),
    [
        ("from shutil import *", True),
        ("from os import *", True),
        ("from sqlite3 import *", True),
        ("from . import *", True),
        ("from lasto.safety.errors import *", True),
        ("from shutil import disk_usage", False),
        ("import os", False),
    ],
)
def test_the_star_import_check(snippet, flagged):
    assert bool(star_imports(ast.parse(snippet))) is flagged


def test_no_star_imports_in_src():
    """A star import binds names no rule here can see: `from shutil import *` then `copy2(...)`, `from os import *`
    then `system(...)`, or `from sqlite3 import *` then `connect(...)` would get past every rule that reads imports
    by name (the copy, child-process, subinterpreter and SQLite rules among them)."""
    found = {name: hits for name, tree in sources().items() if (hits := star_imports(tree))}
    assert found == {}


# Where SQLite's connect lives: the same function in all three, and the Connection class it builds.
SQLITE_MODULES = ("sqlite3", "sqlite3.dbapi2", "_sqlite3")


def _from_sqlite(node: ast.AST, names: dict[str, tuple[str, ...]]) -> bool:
    return any(_rooted_in(node, names, module) for module in SQLITE_MODULES)


def opens_sqlite_connections(tree: ast.Module) -> list[str]:
    """Every way `tree` could open an SQLite connection: connect under any name, a Connection built or subclassed
    (an annotation is fine), or the _sqlite3 module (review finding L14)."""
    names = bindings(tree)
    connection_names = {name for name, binding in names.items() if binding[0] == "from" and binding[1] in SQLITE_MODULES and binding[2] == "Connection"}

    def is_connection_class(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in connection_names
        return isinstance(node, ast.Attribute) and node.attr == "Connection" and _from_sqlite(node.value, names)

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [f"import {alias.name}" for alias in node.names if alias.name == "_sqlite3"]
        elif isinstance(node, ast.ImportFrom) and node.module in SQLITE_MODULES:
            found += [f"from {node.module} import connect" for alias in node.names if alias.name == "connect"]
        elif isinstance(node, ast.Attribute) and node.attr == "connect" and _from_sqlite(node.value, names):
            found.append(f"{ast.unparse(node)}")
        elif isinstance(node, ast.Call) and is_connection_class(node.func):
            found.append(f"{ast.unparse(node.func)}(...)")
        elif isinstance(node, ast.ClassDef) and any(is_connection_class(base) for base in node.bases):
            found.append(f"class {node.name}({', '.join(ast.unparse(base) for base in node.bases)})")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) > 1:
            name = node.args[1]
            if _from_sqlite(node.args[0], names) and isinstance(name, ast.Constant) and name.value in ("connect", "Connection"):
                found.append(f"getattr(..., {name.value!r})")
    return found


@pytest.mark.parametrize(
    ("snippet", "flagged"),
    [
        ("import sqlite3\nsqlite3.connect(path)", True),
        ("import sqlite3 as db\ndb.connect(path)", True),
        ("from sqlite3 import connect\nconnect(path)", True),
        ("from sqlite3 import connect as open_database", True),
        ("import sqlite3\nopener = sqlite3.connect", True),
        ("import sqlite3.dbapi2\nsqlite3.dbapi2.connect(path)", True),
        ("from sqlite3 import dbapi2\ndbapi2.connect(path)", True),
        ("import sqlite3.dbapi2 as dbapi\ndbapi.connect(path)", True),
        ("from sqlite3.dbapi2 import connect", True),
        ("import _sqlite3", True),
        ("import sqlite3\nsqlite3.Connection(path)", True),
        ("from sqlite3 import Connection\nConnection(path)", True),
        ("from sqlite3 import Connection as Plain\nPlain(path)", True),
        ("import sqlite3\nclass Mine(sqlite3.Connection):\n    pass", True),
        ("import sqlite3\ngetattr(sqlite3, 'connect')(path)", True),
        ("import sqlite3\nconn: sqlite3.Connection | None = None", False),
        ("from sqlite3 import Connection\ndef use(conn: Connection) -> None:\n    pass", False),
        ("import sqlite3\nsqlite3.SQLITE_DENY", False),
        ("import sqlite3\nconn.execute('SELECT 1')", False),
        ("import socket\nsocket.create_connection(address)", False),
    ],
)
def test_the_sqlite_connection_check(snippet, flagged):
    assert bool(opens_sqlite_connections(ast.parse(snippet))) is flagged


def test_only_storage_database_opens_sqlite_connections():
    """Review finding L14: lasto's connections refuse ATTACH and VACUUM INTO because storage.database's connect and
    connect_read_only give each one an authorizer. A connection opened anywhere else, by connect or by building a
    Connection, would have none."""
    found = {name: hits for name, tree in sources().items() if (hits := opens_sqlite_connections(tree))}
    assert set(found) == {"lasto.storage.database"}, found


def test_every_argument_parser_disables_abbreviation():
    parsers = 0
    for name, tree in sources().items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called in ("ArgumentParser", "add_parser"):
                parsers += 1
                keywords = {k.arg: k.value for k in node.keywords}
                value = keywords.get("allow_abbrev")
                assert isinstance(value, ast.Constant) and value.value is False, f"{name}: {called} without allow_abbrev=False"
    assert parsers >= 2


SAFETY_INTERNALS = {
    "_channel", "_gate", "_writer", "_can_write", "_transmit", "_pcan", "_functions", "_port",
    "_write_command", "_write_stop", "_command", "_killswitch", "_call",
}  # fmt: skip


def test_nothing_outside_the_safety_core_reaches_into_its_internals():
    for name, tree in sources().items():
        if name.startswith(("lasto.safety", "lasto.sim")):
            continue
        touched = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} & SAFETY_INTERNALS
        assert not touched, f"{name} touches {sorted(touched)}"


def test_no_eval_or_exec():
    for name, tree in sources().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"eval", "exec", "compile", "__import__"}, name


CHILD_PROCESS_MODULES = (
    "subprocess", "_posixsubprocess", "_winapi", "multiprocessing", "concurrent.futures.process", "webbrowser", "pty",
)  # fmt: skip
CHILD_PROCESS_NAMES = {
    "system", "startfile", "popen", "posix_spawn", "posix_spawnp", "CreateProcess", "ProcessPoolExecutor",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
    "create_subprocess_exec", "create_subprocess_shell", "subprocess_exec", "subprocess_shell",
}  # fmt: skip


def _child_process_module(module: str) -> bool:
    return any(module == name or module.startswith(name + ".") for name in CHILD_PROCESS_MODULES)


def starts_a_child_process(tree: ast.Module) -> list[str]:
    """Every import or name in `tree` that could start a child process."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if _child_process_module(alias.name)]
        elif isinstance(node, ast.ImportFrom) and node.module:
            found += [
                f"{node.module}.{alias.name}"
                for alias in node.names
                if _child_process_module(f"{node.module}.{alias.name}") or alias.name in CHILD_PROCESS_NAMES
            ]
        elif isinstance(node, ast.Attribute) and node.attr in CHILD_PROCESS_NAMES:
            found.append(f".{node.attr}")
    return found


@pytest.mark.parametrize(
    ("snippet", "flagged"),
    [
        ("import subprocess", True),
        ("from subprocess import run", True),
        ("import multiprocessing.pool", True),
        ("from concurrent.futures import ProcessPoolExecutor", True),
        ("from concurrent.futures import process", True),
        ("import concurrent.futures.process", True),
        ("import os\nos.system('x')", True),
        ("from os import startfile", True),
        ("import os\nos.spawnv(0, 'x', [])", True),
        ("import os\nos.execv('x', [])", True),
        ("import os\nos.popen('x')", True),
        ("import asyncio\nasyncio.create_subprocess_exec('x')", True),
        ("import webbrowser", True),
        ("import _winapi", True),
        ("import os\nos.environ.get('X')", False),
        ("from concurrent.futures import ThreadPoolExecutor", False),
        ("import threading", False),
    ],
)
def test_the_child_process_check(snippet, flagged):
    assert bool(starts_a_child_process(ast.parse(snippet))) is flagged


# The one import of _winapi src/ may have: guard v2 removes CopyFile2 from it as it installs (Step A review M1,
# approved). Each rule below lets through exactly that import, there, and nothing else.
WINAPI_IMPORT = ("lasto.safety.serial_guard", "import _winapi")


def _but_the_guards_winapi_import(name: str, hits: list[str], spelled: str) -> list[str]:
    return [hit for hit in hits if (name, hit) != (WINAPI_IMPORT[0], spelled)]


def test_nothing_in_src_starts_a_child_process():
    """Guard v2 (Phase 3, A3) refuses a child process at runtime, but not in a test run, which starts them. So src/
    is checked here: code that started one would pass every test and fail only at the truck."""
    found = {
        name: hits
        for name, tree in sources().items()
        if (hits := _but_the_guards_winapi_import(name, starts_a_child_process(tree), "_winapi"))
    }
    assert found == {}


def test_the_guards_winapi_import_is_still_there():
    """The allowance above is used, so it can't outlive the code it was made for."""
    guard = sources()[WINAPI_IMPORT[0]]
    assert "_winapi" in starts_a_child_process(guard)
    assert WINAPI_IMPORT[1] in copies_outside_open(guard)


# What creates a subinterpreter, where none of this interpreter's Python-level audit hooks runs (review finding L13).
# CPython's test modules can too (_testcapi.run_in_subinterp), and none of them raises an audit event in 3.13.
SUBINTERPRETER_MODULES = ("_interpreters", "_xxsubinterpreters", "concurrent.interpreters", "_testcapi", "_testinternalcapi")
# Guard v2 refuses importing those modules at the import statement's `import` event, which these load a module
# without: importlib.import_module, importlib.util.module_from_spec, and _imp itself.
IMPORT_EVENT_BYPASSES = {"import_module", "module_from_spec"}


def creates_a_subinterpreter(tree: ast.Module) -> list[str]:
    """Every import in `tree` that could create a subinterpreter, 3.14's InterpreterPoolExecutor, and every way to load
    a module that the guard's import refusal wouldn't see."""

    def banned(module: str) -> bool:
        return any(module == name or module.startswith(name + ".") for name in (*SUBINTERPRETER_MODULES, "_imp"))

    names = bindings(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if banned(alias.name)]
        elif isinstance(node, ast.ImportFrom) and node.module:
            found += [
                f"{node.module}.{alias.name}"
                for alias in node.names
                if banned(node.module)
                or banned(f"{node.module}.{alias.name}")
                or alias.name == "InterpreterPoolExecutor"
                or (node.module.split(".")[0] == "importlib" and alias.name in IMPORT_EVENT_BYPASSES)
            ]
        elif isinstance(node, ast.Attribute) and node.attr == "InterpreterPoolExecutor":
            found.append(".InterpreterPoolExecutor")
        elif isinstance(node, ast.Attribute) and node.attr in IMPORT_EVENT_BYPASSES and _rooted_in(node.value, names, "importlib"):
            found.append(f"importlib .{node.attr}")
    return found


@pytest.mark.parametrize(
    ("snippet", "flagged"),
    [
        ("import _interpreters", True),
        ("import _interpreters as i", True),
        ("from _interpreters import create", True),
        ("import _xxsubinterpreters", True),
        ("import concurrent.interpreters", True),
        ("from concurrent import interpreters", True),
        ("from concurrent.interpreters import create", True),
        ("from concurrent.futures import InterpreterPoolExecutor", True),
        ("import concurrent.futures\nconcurrent.futures.InterpreterPoolExecutor()", True),
        ("import _testcapi", True),
        ("from _testinternalcapi import get_interp_settings", True),
        ("import importlib\nimportlib.import_module(name)", True),
        ("from importlib import import_module", True),
        ("import importlib.util\nimportlib.util.module_from_spec(spec)", True),
        ("from importlib.util import module_from_spec", True),
        ("from importlib import util\nutil.module_from_spec(spec)", True),
        ("import _imp", True),
        ("from _imp import create_builtin", True),
        ("from concurrent.futures import ThreadPoolExecutor", False),
        ("import concurrent.futures", False),
        ("import threading", False),
    ],
)
def test_the_subinterpreter_check(snippet, flagged):
    assert bool(creates_a_subinterpreter(ast.parse(snippet))) is flagged


def test_nothing_in_src_creates_a_subinterpreter():
    """Review finding L13: a subinterpreter runs code with none of this interpreter's Python-level audit hooks, so
    guard v2 sees nothing it does. The guard refuses importing a module that creates them, at the import statement's
    event; src/ also uses none of the routes that load a module without that event."""
    found = {name: hits for name, tree in sources().items() if (hits := creates_a_subinterpreter(tree))}
    assert found == {}


# shutil's copies: copy2 (and copytree and move, which call it) copy through _winapi.CopyFile2, which raises no audit
# event. pathlib's (Python 3.14) use it too. copyfile goes through open, but copies stay in one place: open.
SHUTIL_COPIES = {"copy", "copy2", "copyfile", "copytree", "move"}
PATH_COPIES = {"copy_into", "move", "move_into"}  # and copy, below: a dict or list has a copy() of its own


def _rooted_in(node: ast.AST, names: dict[str, tuple[str, ...]], module: str) -> bool:
    """Whether an expression is reached through an import of `module` (pathlib.Path, Path, shutil)."""
    while isinstance(node, ast.Attribute | ast.Call | ast.Subscript):
        node = node.func if isinstance(node, ast.Call) else node.value
    binding = names.get(node.id) if isinstance(node, ast.Name) else None
    return binding is not None and binding[1] == module


def copies_outside_open(tree: ast.Module) -> list[str]:
    """Every way `tree` could copy or move a file other than by open(): shutil's copies, _winapi, and pathlib's
    copy and move methods (review finding M1)."""
    names = bindings(tree)
    called_with_arguments = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call) and (node.args or node.keywords)}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [f"import {alias.name}" for alias in node.names if alias.name.split(".")[0] == "_winapi"]
        elif isinstance(node, ast.ImportFrom) and node.module == "_winapi":
            found.append("from _winapi import ...")
        elif isinstance(node, ast.ImportFrom) and node.module == "shutil":
            found += [f"from shutil import {alias.name}" for alias in node.names if alias.name in SHUTIL_COPIES]
        elif isinstance(node, ast.Attribute):
            if node.attr in SHUTIL_COPIES and _rooted_in(node.value, names, "shutil"):
                found.append(f"shutil.{node.attr}")
            elif node.attr in PATH_COPIES:
                found.append(f".{node.attr}")
            elif node.attr == "copy" and not _rooted_in(node.value, names, "copy"):
                # Path(...).copy(target), or pathlib's copy taken as a value; dict.copy() and list.copy() take nothing.
                if id(node) in called_with_arguments or _rooted_in(node.value, names, "pathlib"):
                    found.append(".copy(...)")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) > 1:
            name = node.args[1]
            if isinstance(name, ast.Constant) and name.value in SHUTIL_COPIES | PATH_COPIES:
                found.append(f"getattr(..., {name.value!r})")
    return found


@pytest.mark.parametrize(
    ("snippet", "flagged"),
    [
        ("import shutil\nshutil.copy2(a, b)", True),
        ("import shutil\nshutil.copy(a, b)", True),
        ("import shutil\nshutil.copyfile(a, b)", True),
        ("import shutil\nshutil.copytree(a, b)", True),
        ("import shutil\nshutil.move(a, b)", True),
        ("import shutil as s\ns.copytree(a, b)", True),
        ("import shutil\ncopy = shutil.copy2", True),  # taken as a value
        ("from shutil import copy2", True),
        ("from shutil import move as relocate", True),
        ("import shutil\ngetattr(shutil, 'copy2')(a, b)", True),
        ("import _winapi", True),
        ("import _winapi as w", True),
        ("from _winapi import CopyFile2", True),
        ("from pathlib import Path\nPath(a).copy(b)", True),
        ("from pathlib import Path\nPath(a).copy_into(b)", True),
        ("from pathlib import Path\nPath(a).move(b)", True),
        ("from pathlib import Path\nPath(a).move_into(b)", True),
        ("import pathlib\nfunction = pathlib.Path.copy", True),
        ("from pathlib import Path\nfunction = Path.copy", True),
        ("path.copy(target, preserve_metadata=True)", True),
        ("import shutil\nshutil.disk_usage('.')", False),
        ("import shutil\nshutil.copyfileobj(source, target)", False),  # both already open
        ("import os\nsettings = os.environ.copy()", False),
        ("items = list(values).copy()", False),
        ("import copy\nduplicate = copy.copy(entry)", False),
        ("import copy\nduplicate = copy.deepcopy(entry)", False),
        ("from pathlib import Path\nPath(a).write_bytes(data)", False),
        ("from pathlib import Path\nPath(a).replace(b)", False),
    ],
)
def test_the_copy_check(snippet, flagged):
    assert bool(copies_outside_open(ast.parse(snippet))) is flagged


def test_nothing_in_src_copies_a_file_outside_open():
    """Review finding M1: shutil.copy2 copies through _winapi.CopyFile2, which raises no audit event, so guard v2 never
    sees the destination, which could be a device. copytree and a cross-volume move call copy2, and Python 3.14's
    Path.copy, copy_into, and a cross-volume move or move_into call CopyFile2 for every local file. So src/ copies
    through open(), which the guard checks, and imports nothing from _winapi (but the guard, to remove CopyFile2)."""
    found = {
        name: hits
        for name, tree in sources().items()
        if (hits := _but_the_guards_winapi_import(name, copies_outside_open(tree), WINAPI_IMPORT[1]))
    }
    assert found == {}
