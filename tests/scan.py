"""Static source scanning for the structural safety tests.

Resolves names the way Python would at runtime: an imported name, or an
attribute chain rooted at an imported module (``s.open_active``,
``lasto.safety.pcan_active.x``), is followed through every re-export back to
the module that defines it.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

import lasto

SRC = Path(lasto.__file__).parent
TESTS = Path(__file__).parent

Target = tuple[str, ...]  # ("module", name) or ("name", module, name)


def _module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@cache
def sources() -> dict[str, ast.Module]:
    """Every module under src/lasto, by dotted name."""
    return {
        _module_name(path, SRC.parent): ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in sorted(SRC.rglob("*.py"))
    }


@cache
def test_sources() -> dict[str, ast.Module]:
    """Every test module, by a dotted name under 'tests'."""
    return {
        "tests." + _module_name(path, TESTS): ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in sorted(TESTS.rglob("*.py"))
    }


def bindings(tree: ast.AST) -> dict[str, tuple[str, ...]]:
    """Names bound by imports anywhere in the module (functions included), and what they refer to."""
    found: dict[str, tuple[str, ...]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    found[alias.asname] = ("module", alias.name)
                else:
                    root = alias.name.split(".")[0]
                    found[root] = ("module", root)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                found[alias.asname or alias.name] = ("from", node.module, alias.name)
    return found


def resolve_member(module: str, name: str, depth: int = 0) -> Target:
    """Follow module.name through re-exports to where it is defined."""
    full = f"{module}.{name}"
    if full in sources():
        return ("module", full)
    if module not in sources() or depth > 25:
        return ("name", module, name)
    binding = bindings(_top_level(module)).get(name)
    if binding is None:
        return ("name", module, name)
    return resolve_binding(binding, depth + 1)


@cache
def _top_level(module: str) -> ast.Module:
    """Only the module's top-level statements: what `module.name` can find at runtime."""
    tree = sources()[module]
    top = ast.Module(body=[], type_ignores=[])
    for statement in tree.body:
        if isinstance(statement, ast.Import | ast.ImportFrom | ast.If | ast.Try):
            top.body.append(statement)
    return top


def resolve_binding(binding: tuple[str, ...], depth: int = 0) -> Target:
    if binding[0] == "module":
        return ("module", binding[1])
    _, module, name = binding
    return resolve_member(module, name, depth)


def attribute_chain(node: ast.Attribute) -> list[str] | None:
    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return parts[::-1]


def uses(tree: ast.AST) -> set[Target]:
    """Every lasto module or definition the code reaches through its imports."""
    names = bindings(tree)
    targets = {resolve_binding(binding) for binding in names.values()}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            # `import a.b.c` runs a.b.c and makes it reachable as a.b.c, whatever name it binds.
            targets.update(("module", alias.name) for alias in node.names)
        if not isinstance(node, ast.Attribute):
            continue
        chain = attribute_chain(node)
        if not chain or chain[0] not in names:
            continue
        target = resolve_binding(names[chain[0]])
        for attr in chain[1:]:
            if target[0] != "module":
                break
            target = resolve_member(target[1], attr)
            targets.add(target)
    return targets


def in_safety(target: Target) -> bool:
    module = target[1]
    return module == "lasto.safety" or module.startswith("lasto.safety.")


# ---- the deliberate routes around the safety core ----

BANNED_MODULES = {"ctypes", "gc", "inspect", "importlib"}
BANNED_CALLS = {"__import__", "globals", "vars", "setattr", "delattr", "eval", "exec", "compile"}
NAME_ARGUMENT_CALLS = {"getattr", "hasattr"}
BANNED_DUNDERS = {
    "__dict__", "__closure__", "__globals__", "__code__", "__self__", "__func__", "__wrapped__",
    "__subclasses__", "__mro__", "__bases__", "__builtins__", "__class__", "__new__",
    "__setattr__", "__delattr__", "__getattribute__",
    "__reduce__", "__reduce_ex__", "__getstate__", "__setstate__",
}  # fmt: skip


def deliberate_routes(module: str, tree: ast.AST) -> list[tuple[str, str]]:
    """(route, where) for every construct that could reach around the safety core's guards."""
    found: list[tuple[str, str]] = []
    outside_safety = not (module == "lasto.safety" or module.startswith("lasto.safety."))
    names = bindings(tree)
    for node in ast.walk(tree):
        line = f"{module}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BANNED_MODULES:
                    found.append((root, line))
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in BANNED_MODULES:
                found.append((root, line))
            if node.module == "sys" and any(alias.name == "modules" for alias in node.names):
                found.append(("sys.modules", line))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in BANNED_CALLS:
                found.append((node.func.id, line))
            elif node.func.id in NAME_ARGUMENT_CALLS:
                name = node.args[1] if len(node.args) > 1 else None
                if not (isinstance(name, ast.Constant) and isinstance(name.value, str) and not name.value.startswith("_")):
                    found.append((f"{node.func.id} with a private or computed name", line))
        elif isinstance(node, ast.Attribute):
            chain = attribute_chain(node)
            if chain and len(chain) >= 2 and chain[1] == "modules" and names.get(chain[0]) == ("module", "sys"):
                found.append(("sys.modules", line))
            if node.attr in BANNED_DUNDERS:
                found.append((node.attr, line))
            elif (
                outside_safety
                and node.attr.startswith("_")
                and not node.attr.startswith("__")
                and not (isinstance(node.value, ast.Name) and node.value.id in {"self", "cls"})
            ):
                found.append((f"private attribute .{node.attr}", line))
    return found
