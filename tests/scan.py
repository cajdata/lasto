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
def sources_of_tests() -> dict[str, ast.Module]:
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

# Modules that reach into objects, rewrite code, or rebuild objects from bytes (builtins: rebinding
# `type` or `isinstance` there would defeat checks in the safety core without touching it).
BANNED_MODULES = {
    "ctypes", "_ctypes", "gc", "inspect", "importlib", "builtins",
    "pickle", "marshal", "copyreg", "shelve", "runpy", "code", "codeop",
}  # fmt: skip
BANNED_CALLS = {"__import__", "globals", "vars", "setattr", "delattr", "eval", "exec", "compile"}
NAME_ARGUMENT_CALLS = {"getattr", "hasattr"}
BANNED_DUNDERS = {
    "__dict__", "__closure__", "__globals__", "__code__", "__self__", "__func__", "__wrapped__",
    "__subclasses__", "__mro__", "__bases__", "__base__", "__builtins__", "__class__", "__new__",
    "__setattr__", "__delattr__", "__getattribute__", "__defaults__", "__kwdefaults__",
    "__reduce__", "__reduce_ex__", "__getstate__", "__setstate__", "__traceback__",
}  # fmt: skip
# Frames (whose globals and locals can be changed), tracing hooks, and import hooks that could
# hand the session a different gate when it imports one.
BANNED_ATTRIBUTES = {
    "tb_frame", "f_globals", "f_builtins", "f_locals", "f_back", "gi_frame", "cr_frame", "ag_frame",
    "settrace", "setprofile", "meta_path", "path_hooks", "path_importer_cache",
    "mro",  # the same walk to every base class as __mro__
    # getattr and method calls by a name held in a string, and any dotted name resolved to its object.
    "attrgetter", "methodcaller", "resolve_name",
}  # fmt: skip


def _inside_an_import(node: ast.AST, names: dict[str, tuple[str, ...]]) -> bool:
    """Whether the expression is something an import bound, or reached through one (os.environ, environ,
    ctypes.CDLL.__init__), other than sys.modules, which is a banned route of its own."""
    chain: list[str] = []
    while isinstance(node, ast.Attribute | ast.Subscript):
        if isinstance(node, ast.Attribute):
            chain.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id not in names:
        return False
    return not (chain[-1:] == ["modules"] and names[node.id] == ("module", "sys"))


def _changed_import(node: ast.AST, names: dict[str, tuple[str, ...]]) -> str | None:
    """For a change to something an import bound, what the code changes; otherwise None.

    Changing what a module holds changes it for every user in the process, the safety core included:
    an assignment or deletion (time.monotonic = f, os.environ[key] = value, finding P3), or an in-place
    change (os.environ.update(...), sys.path.insert(...)).
    """
    if isinstance(node, ast.Attribute | ast.Subscript) and isinstance(node.ctx, ast.Store | ast.Del):
        return ast.unparse(node) if _inside_an_import(node, names) else None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in IN_PLACE_METHODS
        and _inside_an_import(node.func.value, names)
    ):
        return f"{ast.unparse(node.func)}(...)"
    return None


def _is_bare_super(node: ast.AST) -> bool:
    """`super()` with no arguments: a class initializing its own new instance through its parent."""
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "super" and not (node.args or node.keywords)


def deliberate_routes(module: str, tree: ast.AST) -> list[tuple[str, str]]:
    """(route, where) for every construct that could reach around the safety core's guards."""
    found: list[tuple[str, str]] = []
    outside_safety = not (module == "lasto.safety" or module.startswith("lasto.safety."))
    names = bindings(tree)
    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for node in ast.walk(tree):
        line = f"{module}:{getattr(node, 'lineno', '?')}"
        changed = _changed_import(node, names)
        if changed is not None:
            found.append((f"change {changed}", line))
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
            # from operator import attrgetter, from sys import settrace: the banned attribute by another name.
            found += [(alias.name, line) for alias in node.names if alias.name in BANNED_ATTRIBUTES]
        elif isinstance(node, ast.Name) and node.id in BANNED_CALLS | NAME_ARGUMENT_CALLS and id(node) not in called:
            # s = setattr, [vars, getattr], f(eval): a banned builtin taken as a value, so its call doesn't show.
            found.append((f"{node.id} as a value", line))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in BANNED_CALLS:
                found.append((node.func.id, line))
            elif node.func.id in NAME_ARGUMENT_CALLS:
                name = node.args[1] if len(node.args) > 1 else None
                if not (isinstance(name, ast.Constant) and isinstance(name.value, str) and not name.value.startswith("_")):
                    found.append((f"{node.func.id} with a private or computed name", line))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "__init__":
            # Calling __init__ on an object that exists rewrites it in place (finding P1); only super().__init__().
            if not _is_bare_super(node.func.value):
                found.append(("__init__", line))
        elif isinstance(node, ast.Attribute):
            chain = attribute_chain(node)
            if chain and len(chain) == 2 and chain[1] == "modules" and names.get(chain[0]) == ("module", "sys"):
                found.append(("sys.modules", line))  # once, at sys.modules itself, not again for sys.modules.pop
            if node.attr in BANNED_DUNDERS or node.attr in BANNED_ATTRIBUTES:
                found.append((node.attr, line))
            elif (
                outside_safety
                and node.attr.startswith("_")
                and not node.attr.startswith("__")
                and not (isinstance(node.value, ast.Name) and node.value.id in {"self", "cls"})
            ):
                found.append((f"private attribute .{node.attr}", line))
    found += [(route, f"{module}:{line} (through {through})") for route, through, line in reexported_routes(tree, names)]
    return found


# Modules also followed through re-exports (review finding L11): the banned ones, and serial, which only
# safety/stn_port.py may import, and only directly.
REEXPORT_WATCHED = BANNED_MODULES | {"serial"}


def _is_lasto(module: str) -> bool:
    return module == "lasto" or module.startswith("lasto.")


def _landing(target: Target) -> str | None:
    """The watched module a resolved name lands in or on (ctypes itself, or ctypes.CDLL), if any."""
    root = target[1].split(".")[0]
    return root if root in REEXPORT_WATCHED else None


def reexported_routes(tree: ast.AST, names: dict[str, tuple[str, ...]]) -> list[tuple[str, str, int]]:
    """(watched module, the lasto module it came through, line) for every imported name or attribute chain that
    reaches a banned module, or serial, through a lasto module's own import of it (review finding L11).

    `from lasto.operations.keep_awake import ctypes`, `keep_awake.ctypes.WinDLL`, and
    `from lasto.safety.hotkey import wintypes` all reach ctypes without an import of ctypes, which is all the
    direct check sees. A direct import, or a chain from one (`ctypes.CDLL`), is left to that check.
    """
    found: dict[tuple[str, int], str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0 and _is_lasto(node.module):
            for alias in node.names:
                landed = _landing(resolve_member(node.module, alias.name))
                if landed is not None:
                    found.setdefault((landed, node.lineno), node.module)
        elif isinstance(node, ast.Attribute):
            chain = attribute_chain(node)
            if not chain or chain[0] not in names:
                continue
            target = resolve_binding(names[chain[0]])
            for attr in chain[1:]:
                if target[0] != "module" or not _is_lasto(target[1]):
                    break
                through = target[1]
                target = resolve_member(through, attr)
                landed = _landing(target)
                if landed is not None:
                    found.setdefault((landed, node.lineno), through)
                    break
    return [(route, through, line) for (route, line), through in found.items()]


# ---- changes to the safety core (finding #2) ----

# Methods that change a container in place.
MUTATING_METHODS = {
    "append", "extend", "insert", "remove", "pop", "popitem", "clear", "update", "setdefault",
    "add", "discard", "difference_update", "intersection_update", "symmetric_difference_update",
    "sort", "reverse", "appendleft", "extendleft", "popleft", "rotate",
    "__iadd__", "__ior__", "__iand__", "__isub__", "__ixor__",
}  # fmt: skip
# Calls whose first argument is what they change: setattr and friends, their dunder forms (called on
# object or type to get past a guard), and pytest's monkeypatch and unittest.mock's patch.
SETTERS = {
    "setattr", "delattr", "setitem", "delitem", "patch", "object",
    "__setattr__", "__delattr__", "__setitem__", "__delitem__",
}  # fmt: skip
# Methods that change what an imported module holds in place (os.environ.update, sys.path.insert).
IN_PLACE_METHODS = MUTATING_METHODS | {"__setitem__", "__delitem__"}


def resolve_expr(node: ast.AST, names: dict[str, tuple[str, ...]]) -> Target | None:
    """What an expression refers to through the module's imports: a module or a member of one, or None."""
    if isinstance(node, ast.Name):
        binding = names.get(node.id)
        return None if binding is None else resolve_binding(binding)
    if isinstance(node, ast.Attribute):
        base = resolve_expr(node.value, names)
        if base is not None and base[0] == "module":
            return resolve_member(base[1], node.attr)
        return base  # an attribute of a member is still part of that member
    if isinstance(node, ast.Subscript):
        return resolve_expr(node.value, names)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"vars", "getattr"} and node.args:
        return resolve_expr(node.args[0], names)
    return None


def _into_safety(node: ast.AST, names: dict[str, tuple[str, ...]]) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value == "lasto.safety" or node.value.startswith("lasto.safety.")  # a patch target by name
    target = resolve_expr(node, names)
    return target is not None and in_safety(target)


def changes_to_the_safety_core(module: str, tree: ast.AST) -> list[tuple[str, str]]:
    """(change, where) for every assignment, deletion, or in-place change to a safety module, class, or member.

    Resolved through imports, re-exports, and attribute chains, so an alias or a
    `from ... import` doesn't hide it. A value the code made itself (a local
    variable, self) doesn't resolve, so changing an instance stays allowed.
    Also, inside the safety core, a `global` statement, which could rebind a
    module's names past its freeze.
    """
    names = bindings(tree)
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        line = f"{module}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Attribute | ast.Subscript) and isinstance(node.ctx, ast.Store | ast.Del):
            if _into_safety(node, names):
                verb = "delete" if isinstance(node.ctx, ast.Del) else "assign"
                found.append((f"{verb} {ast.unparse(node)}", line))
        elif isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if called in SETTERS and node.args and _into_safety(node.args[0], names):
                found.append((f"{ast.unparse(func)}({ast.unparse(node.args[0])}, ...)", line))
            elif isinstance(func, ast.Attribute) and called in MUTATING_METHODS | SETTERS and _into_safety(func.value, names):
                found.append((f"{ast.unparse(func)}(...)", line))
        elif isinstance(node, ast.Global) and in_safety(("module", module)):
            found.append((f"global {', '.join(node.names)}", line))
    return found
