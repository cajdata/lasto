"""Structural rules, checked by reading the source.

- Only safety/pcan_active.py binds or calls CAN_Write (the simulator's fake DLL may list it), and
  only its Writer holds it. Only the gate calls the Writer: the session hands it straight to the gate,
  the gate calls it once in _transmit, and no other safety module names a writer.
- The passive path imports nothing that can transmit and names no write function.
- Only safety/stn_port.py opens a serial port, and no literal in src/ names a serial device path.
- Only the hardware bindings use ctypes.
- Only the session module imports the transmit binding and the gate.
- Every argument parser disables option abbreviation, so nothing shorter than --live can enable it.
- No eval/exec anywhere, no subprocesses in the safety core.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest

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


REFUSAL_TYPES = {
    "SafetyViolation", "KillSwitchTripped", "PassiveModeUnconfirmed", "ValueError", "TypeError", "AdapterError",
    "InterfaceError",
}  # fmt: skip


def test_every_refusal_in_the_safety_core_goes_through_refuse():
    """Rule 11: a refusal is raised only by audit.refuse(), which records it first."""
    raising_sites = 0
    for name, tree in sources().items():
        if not name.startswith("lasto.safety"):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                func = node.exc.func
                raised = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                assert raised not in REFUSAL_TYPES, f"{name} raises {raised} without refuse()"
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
        "lasto.sim.pytest_plugin",  # the test firewall
    }
    users = {name for name, tree in sources().items() if any(i == "ctypes" or i.startswith("ctypes.") for i in imports(tree))}
    assert users <= allowed


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


def test_safety_core_runs_no_subprocesses():
    for name, tree in sources().items():
        if name.startswith("lasto.safety"):
            assert not {"subprocess", "os.system", "multiprocessing"} & imports(tree), name
            assert mentions(tree, "system(") == [], name
