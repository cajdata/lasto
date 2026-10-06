"""Edits to a copy of the app's source for tests, found by name with ast instead of by today's text.

A test that copies a line like `BUILT = frozenset({"drive", "log"})` to edit it breaks the day the
app changes that line, for no reason the test cares about. These find what they edit (a constant's
assignment or value, a call, a statement) by structure, so a test keeps working as the code moves.
Every finder must match exactly one node, so a setup never edits the wrong thing quietly.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable
from pathlib import Path

Find = Callable[[ast.Module], ast.AST]

# A frozen copy of what the site reads from the app (fixtures/README.md). Tests read and edit this,
# never the live app, so their expected values don't move when the app does.
FIXTURE = Path(__file__).parent / "fixtures" / "app"

# Each phase's commands on the roadmap, from the fixture's cli.py and the real roadmap.toml (whose one
# extra is Phase 4's): the phase's own in cli.py's order, then the extras.
FIXTURE_COMMANDS_BY_PHASE = {
    0: (), 1: (), 2: ("lasto drive", "lasto log"), 3: ("lasto map", "lasto verify"),
    4: ("lasto snapshot", "lasto identify", "lasto view", "lasto drive --profile"),
    5: ("lasto decode",), 6: ("lasto discover",), 7: (), 8: ("lasto report", "lasto export"), 9: ("lasto gui",),
}


def copy_fixture(root: Path, rels: Iterable[str]) -> Path:
    """Copy these files of the frozen fixture to root/src/lasto, for a test to edit."""
    for rel in rels:
        dst = _file(root, rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(_file(FIXTURE, rel).read_bytes())
    return root


def _file(root: Path, rel: str) -> Path:
    return root / "src" / "lasto" / rel


def one(nodes: Iterable[ast.AST], what: str) -> ast.AST:
    found = list(nodes)
    assert len(found) == 1, f"test setup: expected one {what}, found {len(found)}"
    return found[0]


def _index(lines: list[str], lineno: int, col: int) -> int:
    """A str index from ast's 1-based line and UTF-8 byte column."""
    line = lines[lineno - 1]
    return sum(len(x) for x in lines[: lineno - 1]) + len(line.encode("utf-8")[:col].decode("utf-8"))


def _span(text: str, node: ast.AST) -> tuple[int, int, str]:
    # Lines end only at \n, as ast counts them; str.splitlines also breaks at a form feed or U+2028.
    lines = re.split(r"(?<=\n)", text)
    start = _index(lines, node.lineno, node.col_offset)  # type: ignore[attr-defined]
    end = _index(lines, node.end_lineno, node.end_col_offset)  # type: ignore[attr-defined]
    indent = lines[node.lineno - 1][: len(lines[node.lineno - 1]) - len(lines[node.lineno - 1].lstrip())]  # type: ignore[attr-defined]
    return start, end, indent


def _apply(root: Path, rel: str, find: Find, change: Callable[[str, int, int, str], str]) -> None:
    path = _file(root, rel)
    text = path.read_text(encoding="utf-8")
    start, end, indent = _span(text, find(ast.parse(text)))
    path.write_text(change(text, start, end, indent), encoding="utf-8")


def replace(root: Path, rel: str, find: Find, make: Callable[[str], str]) -> None:
    """Replace the node `find` returns with make(its source)."""
    _apply(root, rel, find, lambda text, start, end, _indent: text[:start] + make(text[start:end]) + text[end:])


def nest(root: Path, rel: str, find: Find, header: str) -> None:
    """Indent the statement `find` returns one level, under `header` (like "if True:")."""
    def change(text: str, start: int, end: int, indent: str) -> str:
        body = text[start:end].replace("\n", "\n    ")
        return f"{text[:start]}{header}\n{indent}    {body}{text[end:]}"
    _apply(root, rel, find, change)


def insert_before(root: Path, rel: str, find: Find, lines: str) -> None:
    """New lines before the statement `find` returns, at its indentation."""
    def change(text: str, start: int, end: int, indent: str) -> str:
        block = "\n".join(lines.splitlines()).replace("\n", "\n" + indent)
        return f"{text[:start]}{block}\n{indent}{text[start:]}"
    _apply(root, rel, find, change)


def insert_after(root: Path, rel: str, find: Find, lines: str) -> None:
    """New lines after the statement `find` returns, at its indentation."""
    def change(text: str, start: int, end: int, indent: str) -> str:
        block = "\n".join(lines.splitlines()).replace("\n", "\n" + indent)
        return f"{text[:end]}\n{indent}{block}{text[end:]}"
    _apply(root, rel, find, change)


def with_keyword(keyword: str) -> Callable[[str], str]:
    """For replace() on a call: the same call with one more keyword argument, however the call is wrapped."""
    def make(call: str) -> str:
        inside = call.rstrip()[:-1].rstrip()  # without the closing parenthesis
        return f"{inside.rstrip(',')}, {keyword})"
    return make


def append(root: Path, rel: str, lines: str) -> None:
    """New top-level lines at the end of the file."""
    path = _file(root, rel)
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n") + "\n\n\n" + lines.strip("\n") + "\n", encoding="utf-8")


# Finders


def assignment(name: str) -> Find:
    """The one top-level assignment of `name`."""
    def find(tree: ast.Module) -> ast.AST:
        return one((s for s in tree.body if (isinstance(s, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in s.targets))
                    or (isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name) and s.target.id == name)), f"assignment of {name}")
    return find


def value(name: str) -> Find:
    """The value assigned to `name` at the top level."""
    return lambda tree: assignment(name)(tree).value  # type: ignore[attr-defined]


def value_source(root: Path, rel: str, name: str) -> str:
    text = _file(root, rel).read_text(encoding="utf-8")
    return ast.get_source_segment(text, value(name)(ast.parse(text))) or ""


def rebind(root: Path, rel: str, name: str, make: Callable[[str], str]) -> None:
    """name = make(its current value's source), so a test never depends on today's value."""
    replace(root, rel, value(name), make)


def function(name: str) -> Callable[[ast.Module], ast.FunctionDef]:
    return lambda tree: one((s for s in tree.body if isinstance(s, ast.FunctionDef) and s.name == name), f"def {name}")  # type: ignore[return-value]


def within(func: str, match: Callable[[ast.AST], bool], what: str) -> Find:
    """The one node inside def `func` that `match` accepts."""
    return lambda tree: one((n for n in ast.walk(function(func)(tree)) if match(n)), what)


def call_with_first_arg(method: str, first: str) -> Callable[[ast.AST], bool]:
    """A call like x.add_argument("--profile", ...)."""
    return lambda n: (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == method
                      and bool(n.args) and isinstance(n.args[0], ast.Constant) and n.args[0].value == first)


def statement_of(match: Callable[[ast.AST], bool]) -> Callable[[ast.AST], bool]:
    """An expression statement whose expression `match` accepts."""
    return lambda n: isinstance(n, ast.Expr) and match(n.value)


def if_testing(test: str) -> Callable[[ast.AST], bool]:
    """An if statement whose test reads exactly `test`, like `name == "log"`."""
    return lambda n: isinstance(n, ast.If) and ast.unparse(n.test) == ast.unparse(ast.parse(test, mode="eval").body)
