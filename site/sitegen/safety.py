"""Facts about the safety core and CLI, read from their source with ast.

The build never imports lasto. It parses the source files and evaluates only literal
constants (numbers, strings, sets, dicts, frozenset(...), and | between known sets), so
nothing here can execute app code or reach hardware. Every number the site shows about
what Lasto may send comes from here, so the site can't drift from the code.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from sitegen import paths
from sitegen.data import BuildError, Roadmap


@dataclass(frozen=True)
class SourceRef:
    file: str  # repo-relative, forward slashes
    line: int
    end_line: int


class _Symbol(str):
    """A dotted name such as Purpose.LOGGING, kept as text."""


MAX_BITS = 4096  # no whole number the site reads comes near this; chained powers or products could hang the build


def _cap_bits(bits: int) -> None:
    if bits > MAX_BITS:
        raise BuildError(f"a whole number over {MAX_BITS} bits isn't a constant the site reads")


def _hashed(make: Callable[[], object]) -> object:
    """A set, dict, or frozenset literal, refused as a BuildError if an element can't be hashed."""
    try:
        return make()
    except TypeError as exc:  # frozenset({Ecu(name=...)}), say: the constructor call reads as a dict
        raise BuildError(f"not a literal the site can read: {exc}") from None


def _evaluate(node: ast.AST, env: dict[str, object]) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        operand = _evaluate(node.operand, env)
        if not isinstance(operand, (int, float)) or isinstance(operand, bool):
            raise BuildError("unary - only on numbers")
        return -operand
    if isinstance(node, ast.Set):
        return _hashed(lambda: {_evaluate(e, env) for e in node.elts})
    if isinstance(node, ast.Tuple):
        return tuple(_evaluate(e, env) for e in node.elts)
    if isinstance(node, ast.List):
        return [_evaluate(e, env) for e in node.elts]
    if isinstance(node, ast.Dict):
        if any(k is None for k in node.keys):
            raise BuildError("dict ** unpacking isn't a literal")
        return _hashed(lambda: {_evaluate(k, env): _evaluate(v, env) for k, v in zip(node.keys, node.values, strict=True)})
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        raise BuildError(f"unknown name {node.id}")
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return _Symbol(f"{node.value.id}.{node.attr}")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left, right = _evaluate(node.left, env), _evaluate(node.right, env)
        if isinstance(left, frozenset) and isinstance(right, frozenset):
            return left | right
        if isinstance(left, int) and isinstance(right, int) and not isinstance(left, bool) and not isinstance(right, bool):
            return left | right
        raise BuildError("| only between sets or between integers")
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        left, right = _evaluate(node.left, env), _evaluate(node.right, env)
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (left, right)):
            raise BuildError("arithmetic only between numbers")
        if isinstance(node.op, ast.Mult) and isinstance(left, int) and isinstance(right, int):
            _cap_bits(left.bit_length() + right.bit_length())
        ops = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b}
        try:
            return ops[type(node.op)](left, right)
        except ArithmeticError as exc:
            raise BuildError(f"arithmetic failed: {exc}") from None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
        # Only a number to a small whole power (2**30, say), so nothing in the source can make the build hang.
        base, exponent = _evaluate(node.left, env), _evaluate(node.right, env)
        if not isinstance(base, (int, float)) or isinstance(base, bool):
            raise BuildError("** only on numbers")
        if type(exponent) is not int or not 0 <= exponent <= 64:
            raise BuildError("** only to a whole power from 0 to 64")
        if isinstance(base, int):
            _cap_bits(abs(base).bit_length() * exponent)
        try:
            return base**exponent
        except ArithmeticError as exc:  # a float base can still overflow, 1e308**2 say
            raise BuildError(f"** failed: {exc}") from None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        # The only calls evaluated: frozenset(...), MappingProxyType(...) (a read-only view of a dict literal),
        # and int(...) of a number. Anything else stays unevaluated, so nothing in the source can run.
        if node.func.id == "frozenset" and len(node.args) == 1 and not node.keywords:
            return _hashed(lambda: frozenset(_evaluate(node.args[0], env)))  # type: ignore[arg-type]
        if node.func.id == "MappingProxyType" and len(node.args) == 1 and not node.keywords:
            if not isinstance(node.args[0], ast.Dict):
                raise BuildError("MappingProxyType of something that isn't a dict literal")
            return _evaluate(node.args[0], env)
        if node.func.id == "int" and len(node.args) == 1 and not node.keywords:
            inner = _evaluate(node.args[0], env)
            if not isinstance(inner, (int, float)) or isinstance(inner, bool):
                raise BuildError("int() of something that isn't a number")
            try:
                return int(inner)  # truncates, as the app's own int() does
            except (ArithmeticError, ValueError) as exc:
                raise BuildError(f"int() failed: {exc}") from None
        if not node.args:
            # A constructor call with keyword arguments, such as Ecu(name=..., ...).
            return {"__type__": node.func.id, **{k.arg: _evaluate(k.value, env) for k in node.keywords if k.arg}}
    raise BuildError(f"not a literal: {ast.dump(node)[:80]}")


@dataclass
class _Module:
    path: Path
    root: Path
    tree: ast.Module | None = None
    values: dict[str, object] = field(default_factory=dict)
    refs: dict[str, SourceRef] = field(default_factory=dict)
    # Names bound in the module's namespace more than once (by assignment, import, def, class,
    # except ... as, a match capture, or :=), augmented (|=), deleted, bound inside a top-level
    # loop or branch, or declared global, and any constant computed from one of those. Their
    # top-level value may not be the one the app uses, so the site refuses to read them.
    unstable: set[str] = field(default_factory=set)

    def get(self, name: str) -> object:
        if name in self.unstable or "*" in self.unstable:  # an `import *` could bring in any name
            raise BuildError(
                f"{self.rel} changes {name} after defining it, or computes it from a name that changes; "
                "the site can only read single literal constants"
            )
        if name not in self.values:
            raise BuildError(f"{self.rel} no longer defines {name} as a literal constant; update sitegen/safety.py")
        return self.values[name]

    @property
    def rel(self) -> str:
        return self.path.relative_to(self.root).as_posix()


_OWN_SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_COMPOUND = (ast.If, ast.Try, ast.TryStar, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Match)


def _names_bound(node: ast.AST) -> list[str]:
    """The names one node binds or deletes, in each of Python's forms other than :=."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return [node.id]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [a.asname or a.name.split(".")[0] for a in node.names]  # "*" for `import *`
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.ExceptHandler) and node.name:
        return [node.name]
    if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
        return [node.name]
    if isinstance(node, ast.MatchMapping) and node.rest:
        return [node.rest]
    return []


# Methods that change a dict, set, or list in place.
_MUTATORS = frozenset({
    "update", "pop", "popitem", "clear", "setdefault", "add", "discard", "remove", "append", "extend", "insert",
    "sort", "reverse", "difference_update", "intersection_update", "symmetric_difference_update",
    "__setitem__", "__delitem__", "__setattr__", "__delattr__",
})


def _root(node: ast.AST) -> str | None:
    """X for X[0], X.a, X.a[0]."""
    while isinstance(node, (ast.Subscript, ast.Attribute)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _module_bindings(node: ast.AST, in_comprehension: bool = False) -> Iterator[tuple[str, bool]]:
    """(name, always unstable) for every binding below `node` that lands in the module's namespace.

    Function, lambda, and class bodies have their own scope, and so do a comprehension's loop
    variables, but a := inside a comprehension binds in the module, and so does one in a decorator,
    a default, or a class base. A :=, a del, or a change in place (X[0] = ..., X.update(...)) at
    module level makes the name unstable however often it's bound."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.NamedExpr):
            yield child.target.id, True
            yield from _module_bindings(ast.Expr(value=child.value), in_comprehension)  # the value itself too
            continue
        if isinstance(child, (ast.Subscript, ast.Attribute)) and isinstance(child.ctx, (ast.Store, ast.Del)):
            root = _root(child)
            if root:
                yield root, True
        if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr in _MUTATORS
                and _root(child.func.value)):
            yield _root(child.func.value), True  # type: ignore[misc]
        if not in_comprehension:
            deleting = isinstance(child, ast.Name) and isinstance(child.ctx, ast.Del)
            yield from ((name, deleting) for name in _names_bound(child))
        if isinstance(child, _OWN_SCOPE):
            # The body has its own scope; decorators, defaults, bases, and annotations run in the module's.
            for field_name, value in ast.iter_fields(child):
                if field_name in ("body", "type_params"):
                    continue
                for item in value if isinstance(value, list) else [value]:
                    if isinstance(item, ast.AST):
                        yield from _module_bindings(ast.Expr(value=item), in_comprehension)  # type: ignore[arg-type]
            continue
        yield from _module_bindings(child, in_comprehension or isinstance(child, _COMPREHENSIONS))


def _in_scope(scope: ast.AST) -> Iterator[ast.AST]:
    """The nodes in a module's, function's, lambda's, or class's own body; nested ones are scopes of their own."""
    body = scope.body  # type: ignore[attr-defined]
    stack = list(body) if isinstance(body, list) else [body]
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _OWN_SCOPE):
            stack.extend(ast.iter_child_nodes(node))


def _changed_in_place(node: ast.AST) -> ast.AST | None:
    """For X[0] = ..., del X.a, or X.update(...), the X (or globals()) being changed; else None."""
    if isinstance(node, (ast.Subscript, ast.Attribute)) and isinstance(node.ctx, (ast.Store, ast.Del)):
        base: ast.AST = node
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _MUTATORS:
        base = node.func.value
    else:
        return None
    while isinstance(base, (ast.Subscript, ast.Attribute)):
        base = base.value
    return base


def _changed_names(tree: ast.Module) -> set[str]:
    """Names changed in place anywhere in the file: at module level, or in a function or class body
    (a function the module calls can change a constant), unless the name is that scope's own.
    "*" if globals() or vars() is changed, since then any name could be."""
    changed: set[str] = set()
    for scope in [tree, *(n for n in ast.walk(tree) if isinstance(n, _OWN_SCOPE))]:
        nodes = list(_in_scope(scope))
        own: set[str] = set()
        if scope is not tree:
            own = {n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                a = scope.args
                own |= {p.arg for p in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg] if p}
        for node in nodes:
            base = _changed_in_place(node)
            if isinstance(base, ast.Call) and isinstance(base.func, ast.Name) and base.func.id in ("globals", "vars"):
                changed.add("*")
            elif isinstance(base, ast.Name) and base.id not in own:
                changed.add(base.id)
    return changed


def _read(path: Path, root: Path) -> _Module:
    if not path.exists():
        raise BuildError(f"can't find {path}")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    mod = _Module(path, root, tree)
    # A name bound more than once in the module's namespace, by any statement (assignment, import,
    # def, class, except ... as, a match capture, :=, del), may not have the value the site reads.
    counts: dict[str, int] = {}
    for name, always in _module_bindings(tree):
        counts[name] = counts.get(name, 0) + 1
        if always:
            mod.unstable.add(name)
    mod.unstable |= {name for name, count in counts.items() if count > 1}
    if "*" in counts:
        mod.unstable.add("*")  # `import *` could bring in any name
    mod.unstable |= _changed_names(tree)
    for node in ast.walk(tree):
        # x |= ..., anywhere, or a global/nonlocal declaration that lets a function rebind a constant.
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            mod.unstable.add(node.target.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            mod.unstable.update(node.names)
    # Names bound inside module-level if/try/for/while/with/match blocks.
    for stmt in tree.body:
        if isinstance(stmt, _COMPOUND):
            mod.unstable.update(name for name, _ in _module_bindings(ast.Module(body=[stmt], type_ignores=[])))
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            name, value = stmt.targets[0].id, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            name, value = stmt.target.id, stmt.value
        else:
            continue
        if any(isinstance(n, ast.Name) and n.id in mod.unstable for n in ast.walk(value)):
            mod.unstable.add(name)  # computed from a name that changes, so it may too (in source order, so it carries on)
        try:
            mod.values[name] = _evaluate(value, mod.values)
        except BuildError:
            continue  # not a literal; only a problem if the site asks for it
        mod.refs[name] = SourceRef(mod.rel, stmt.lineno, stmt.end_lineno or stmt.lineno)
    return mod


def _ints(value: object, what: str) -> frozenset[int]:
    if not isinstance(value, (set, frozenset)) or not all(isinstance(v, int) for v in value):
        raise BuildError(f"{what} isn't a set of integers")
    return frozenset(value)


def _num(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BuildError(f"{what} isn't a number")
    return value


def _count(value: object, what: str) -> int:
    """A whole number: an int, or a float with nothing after the point. Never rounded, so the site can't
    show a limit the app doesn't enforce."""
    number = _num(value, what)
    if isinstance(number, float) and not number.is_integer():
        raise BuildError(f"{what} is {number}, which isn't a whole number")
    return int(number)


@dataclass(frozen=True)
class Ecu:
    name: str
    kind: str
    request_id: int
    response_id: int


@dataclass(frozen=True)
class SafetyFacts:
    obd_services: frozenset[int]
    manufacturer_services: frozenset[int]
    never_services: frozenset[int]
    dtc_read_services: frozenset[int]
    probe_pids: frozenset[int]
    functional_id: int
    padding_byte: int
    ecus: tuple[Ecu, ...]
    rates: dict[str, float]
    ceiling: float
    backoff_start: float
    backoff_max: float
    nrc_consecutive: int
    nrc_window_limit: int
    nrc_window_seconds: float
    timeout_limit: int
    min_engine_off_voltage: float
    response_pending_wait: float
    response_pending_max: int
    listen_window: float
    ceiling_frames: int
    ceiling_window: float
    max_rate_wait: float
    status_interval: float
    reopen_attempts: int
    max_reopens: int
    hotkey: str
    commands: dict[str, tuple[str, int]]
    refs: dict[str, SourceRef]
    cli_options: frozenset[str] = frozenset()  # every --option cli.py's parsers define
    # Each command's own --options, --help included; "" is bare `lasto` (--version, --help).
    cli_options_by_command: dict[str, frozenset[str]] = field(default_factory=dict)

    @property
    def allowed_services(self) -> frozenset[int]:
        return self.obd_services | self.manufacturer_services

    def ref(self, name: str) -> SourceRef:
        return self.refs[name]


_MODIFIERS = {"MOD_CONTROL": "Ctrl", "MOD_ALT": "Alt", "MOD_SHIFT": "Shift", "MOD_WIN": "Win"}


def _hotkey(hot: _Module) -> str:
    """The key combination hotkey.py actually registers, read from its RegisterHotKey call."""
    calls = [
        n for n in ast.walk(hot.tree) if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute) and n.func.attr == "RegisterHotKey"
    ]
    if len(calls) != 1 or len(calls[0].args) != 4:
        raise BuildError("hotkey.py should make exactly one RegisterHotKey(hwnd, id, modifiers, key) call")
    mods_node, key_node = calls[0].args[2], calls[0].args[3]
    names: list[str] = []
    for n in ast.walk(mods_node):
        if isinstance(n, ast.Name):
            names.append(n.id)
        elif not isinstance(n, (ast.BinOp, ast.BitOr, ast.Load)):
            raise BuildError("RegisterHotKey's modifiers should be MOD_* names joined with |")
    labels = [_MODIFIERS[n] for n in ("MOD_CONTROL", "MOD_ALT", "MOD_SHIFT", "MOD_WIN") if n in names]
    if not isinstance(key_node, ast.Name) or not key_node.id.startswith("VK_"):
        raise BuildError("RegisterHotKey's key should be a VK_* constant")
    code = hot.get(key_node.id)
    if not isinstance(code, int) or not (0x30 <= code <= 0x5A):
        raise BuildError(f"{key_node.id} isn't a letter or digit key")
    for n in names:
        hot.get(n)  # each modifier must be a stable literal too
    if not labels:
        raise BuildError("the kill-switch hotkey has no modifier keys")
    return "+".join([*labels, chr(code)])


def _is_add_argument(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"


def _options(nodes: list[ast.AST]) -> set[str]:
    """The --option strings of every add_argument call under these nodes."""
    return {
        arg.value
        for top in nodes
        for node in ast.walk(top)
        if _is_add_argument(node)
        for arg in node.args  # type: ignore[attr-defined]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("--")
    }


def _options_by_command(cli: _Module, commands: dict[str, tuple[str, int]]) -> dict[str, frozenset[str]]:
    """Each command's --options, read from build_parser's loop over COMMANDS.

    Only the shapes cli.py uses are understood: add_argument on the top-level parser, and inside
    the loop either for every command or under `if name in SOME_SET` / `if name == "cmd"`. Any
    other shape fails the build, so a refactor can't make the site check options against the
    wrong command. argparse gives every parser --help.
    """
    def unclear(what: str) -> BuildError:
        return BuildError(f"cli.py's build_parser {what}; update sitegen/safety.py to read it")

    if "build_parser" in cli.unstable:
        raise unclear("is defined more than once")
    build = next((n for n in cli.tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_parser"), None)  # type: ignore[union-attr]
    if build is None:
        raise unclear("is gone")

    # Keywords that don't change which options a parser accepts. add_help, parents, aliases, and the
    # rest would, so they're refused.
    plain = {
        "ArgumentParser": {"prog", "description", "epilog", "usage", "allow_abbrev", "formatter_class"},
        "add_subparsers": {"dest", "required", "metavar", "title", "description", "help"},
        "add_parser": {"help", "description", "epilog", "usage", "allow_abbrev", "formatter_class"},
    }

    def assigned_call(stmt: ast.stmt, method: str, on: str | None = None) -> str | None:
        """The name in `name = <obj>.<method>(...)`, if stmt is that and every keyword is a plain one.
        `on` names the object it must be called on; ArgumentParser may be argparse.ArgumentParser or bare."""
        if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)
                and isinstance(stmt.value, ast.Call)):
            return None
        call, func = stmt.value, stmt.value.func
        if isinstance(func, ast.Attribute) and func.attr == method:
            if on is not None and not (isinstance(func.value, ast.Name) and func.value.id == on):
                return None
        elif not (on is None and isinstance(func, ast.Name) and func.id == method):
            return None
        odd = [k.arg or "**" for k in call.keywords if k.arg not in plain[method]]
        if odd:
            raise unclear(f"passes {', '.join(odd)} to {method}() (line {stmt.lineno})")
        return stmt.targets[0].id

    def add_call(stmt: ast.stmt, receiver: str) -> ast.Call | None:
        """The call in a bare `receiver.add_argument(...)` statement, if stmt is that."""
        if isinstance(stmt, ast.Expr) and _is_add_argument(stmt.value):
            func = stmt.value.func  # type: ignore[attr-defined]
            if isinstance(func.value, ast.Name) and func.value.id == receiver:
                return stmt.value  # type: ignore[return-value]
        return None

    def options_of(call: ast.Call) -> set[str]:
        return {a.value for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value.startswith("--")}

    # build_parser: parser = argparse.ArgumentParser(...), parser.add_argument(...) for bare `lasto`,
    # subparsers = parser.add_subparsers(...), the loop, and return parser. Nothing else.
    parser = subparsers = None
    top: set[str] = set()
    loop: ast.For | None = None
    for stmt in build.body:
        if parser is None and (name := assigned_call(stmt, "ArgumentParser")):
            parser = name
        elif parser and (call := add_call(stmt, parser)):
            top |= options_of(call)
        elif parser and subparsers is None and (name := assigned_call(stmt, "add_subparsers", on=parser)):
            subparsers = name
        elif isinstance(stmt, ast.For) and loop is None:
            loop = stmt
        elif isinstance(stmt, ast.Return) or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)):
            pass
        else:
            raise unclear(f"has a statement the site can't follow (line {stmt.lineno})")
    if parser is None or subparsers is None or loop is None:
        raise unclear("no longer makes a parser, its subparsers, and one loop over COMMANDS")
    if loop.orelse:
        raise unclear(f"has an else on its loop (line {loop.lineno})")
    it, target = loop.iter, loop.target
    if not (isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute) and it.func.attr == "items"
            and isinstance(it.func.value, ast.Name) and it.func.value.id == "COMMANDS"
            and isinstance(target, ast.Tuple) and target.elts and isinstance(target.elts[0], ast.Name)):
        raise unclear("no longer loops `for name, ... in COMMANDS.items()`")
    var = target.elts[0].id

    def chosen(test: ast.expr) -> set[str]:
        if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.left, ast.Name) and test.left.id == var:
            op, right = test.ops[0], test.comparators[0]
            if isinstance(op, ast.In) and isinstance(right, ast.Name):
                names = cli.get(right.id)
                if isinstance(names, (set, frozenset)) and all(isinstance(n, str) for n in names):
                    picked = set(names)
                    if picked - commands.keys():
                        raise unclear(f"gives options to {sorted(picked - commands.keys())}, which aren't commands")
                    return picked
            if isinstance(op, ast.Eq) and isinstance(right, ast.Constant) and right.value in commands:
                return {right.value}
        raise unclear(f"adds options under a test the site can't follow (line {test.lineno})")

    # The loop: sub = subparsers.add_parser(name, ...) first, then sub.add_argument(...) calls, bare or
    # in an `if` with no else whose body is only those calls. Nothing else, so no continue, break,
    # nested block, other parser, or parents= can make an option land on the wrong command.
    body = loop.body
    sub = assigned_call(body[0], "add_parser", on=subparsers) if body else None
    if sub is None:
        raise unclear(f"no longer starts its loop with `sub = {subparsers}.add_parser(name, ...)`")
    every: set[str] = set()
    some: dict[str, set[str]] = {c: set() for c in commands}
    for stmt in body[1:]:
        if call := add_call(stmt, sub):
            every |= options_of(call)
        elif isinstance(stmt, ast.If) and not stmt.orelse:
            calls = [add_call(s, sub) for s in stmt.body]
            if not all(calls):
                raise unclear(f"has something other than {sub}.add_argument(...) calls in an if (line {stmt.lineno})")
            for command in chosen(stmt.test):
                for call in calls:
                    some[command] |= options_of(call)  # type: ignore[arg-type]
        else:
            raise unclear(f"has a statement the site can't follow in its loop (line {stmt.lineno})")
    by = {"": frozenset({"--help"} | top)}
    by.update({c: frozenset({"--help"} | every | some[c]) for c in commands})
    return by


def load_facts(root: Path | None = None) -> SafetyFacts:
    base = root or paths.ROOT
    safety = base / "src" / "lasto" / "safety"
    policy = _read(safety / "policy.py", base)
    ecus = _read(safety / "ecus.py", base)
    rate = _read(safety / "ratelimit.py", base)
    kill = _read(safety / "killswitch.py", base)
    inter = _read(safety / "interlocks.py", base)
    gate = _read(safety / "gate.py", base)
    hot = _read(safety / "hotkey.py", base)
    session = _read(safety / "session.py", base)
    reader = _read(safety / "reader.py", base)
    cli = _read(base / "src" / "lasto" / "cli.py", base)

    obd = _ints(policy.get("OBD_SERVICES"), "OBD_SERVICES")
    mfr = _ints(policy.get("MANUFACTURER_READ_SERVICES"), "MANUFACTURER_READ_SERVICES")
    never = _ints(policy.get("NEVER_SERVICES"), "NEVER_SERVICES")
    allowed = _ints(policy.get("ALLOWED_SERVICES"), "ALLOWED_SERVICES")
    if allowed != obd | mfr:
        raise BuildError("ALLOWED_SERVICES is no longer OBD_SERVICES | MANUFACTURER_READ_SERVICES")
    if allowed & never:
        raise BuildError(f"services both allowed and never allowed: {sorted(allowed & never)}")

    approved = ecus.get("APPROVED_ECUS")
    if not isinstance(approved, tuple) or not all(isinstance(e, dict) and e.get("__type__") == "Ecu" for e in approved):
        raise BuildError("APPROVED_ECUS isn't a tuple of Ecu(...) literals")
    ecu_list = tuple(
        Ecu(str(e["name"]), str(e["kind"]).split(".")[-1].lower(), int(e["request_id"]), int(e["response_id"]))
        for e in approved
    )

    raw_rates = rate.get("PURPOSE_RATES")
    if not isinstance(raw_rates, dict):
        raise BuildError("PURPOSE_RATES isn't a dict")
    rates = {str(k).split(".")[-1].lower(): _num(v, f"rate {k}") for k, v in raw_rates.items()}
    ceiling = _num(rate.get("HARD_CEILING_PER_SECOND"), "HARD_CEILING_PER_SECOND")
    if any(r > ceiling for r in rates.values()):
        raise BuildError("a purpose rate is above the hard ceiling")
    ceiling_frames = _count(rate.get("CEILING_FRAMES"), "CEILING_FRAMES")
    ceiling_window = _num(rate.get("CEILING_WINDOW"), "CEILING_WINDOW")
    if not ceiling_window > 0:
        raise BuildError(f"CEILING_WINDOW is {ceiling_window}; the write function's window has to be longer than 0 s")
    if ceiling_frames / ceiling_window > ceiling:
        raise BuildError("the write function's backstop is looser than the hard ceiling")

    hotkey = _hotkey(hot)

    raw_commands = cli.get("COMMANDS")
    if not isinstance(raw_commands, dict):
        raise BuildError("cli.py COMMANDS isn't a dict")
    commands = {str(k): (str(v[0]), int(v[1])) for k, v in raw_commands.items()}
    # Every option string passed to an add_argument call, such as "--live", read from the source like the rest.
    cli_options = frozenset(_options([cli.tree]))  # type: ignore[list-item]
    if not cli_options:
        raise BuildError("cli.py defines no --options any more; update sitegen/safety.py")
    cli_options |= {"--help"}  # argparse gives every parser one
    cli_options_by_command = _options_by_command(cli, commands)

    refs: dict[str, SourceRef] = {}
    for mod in (policy, ecus, rate, kill, inter, gate, hot, session, reader, cli):
        for name, ref in mod.refs.items():
            refs.setdefault(name, ref)

    return SafetyFacts(
        obd_services=obd,
        manufacturer_services=mfr,
        never_services=never,
        dtc_read_services=_ints(policy.get("DTC_READ_SERVICES"), "DTC_READ_SERVICES"),
        probe_pids=_ints(policy.get("PROBE_PIDS"), "PROBE_PIDS"),
        functional_id=_count(policy.get("FUNCTIONAL_REQUEST_ID"), "FUNCTIONAL_REQUEST_ID"),
        padding_byte=_count(policy.get("PADDING_BYTE"), "PADDING_BYTE"),
        ecus=ecu_list,
        rates=rates,
        ceiling=ceiling,
        backoff_start=_num(rate.get("BACKOFF_START"), "BACKOFF_START"),
        backoff_max=_num(rate.get("BACKOFF_MAX"), "BACKOFF_MAX"),
        nrc_consecutive=_count(kill.get("CONSECUTIVE_NRC_LIMIT"), "CONSECUTIVE_NRC_LIMIT"),
        nrc_window_limit=_count(kill.get("WINDOW_NRC_LIMIT"), "WINDOW_NRC_LIMIT"),
        nrc_window_seconds=_num(kill.get("NRC_WINDOW_SECONDS"), "NRC_WINDOW_SECONDS"),
        timeout_limit=_count(kill.get("CONSECUTIVE_TIMEOUT_LIMIT"), "CONSECUTIVE_TIMEOUT_LIMIT"),
        min_engine_off_voltage=_num(inter.get("MIN_ENGINE_OFF_VOLTAGE"), "MIN_ENGINE_OFF_VOLTAGE"),
        response_pending_wait=_num(gate.get("P2_STAR_TIMEOUT"), "P2_STAR_TIMEOUT"),
        response_pending_max=_count(gate.get("MAX_RESPONSE_PENDING"), "MAX_RESPONSE_PENDING"),
        listen_window=_num(session.get("LISTEN_WINDOW"), "LISTEN_WINDOW"),
        ceiling_frames=ceiling_frames,
        ceiling_window=ceiling_window,
        max_rate_wait=_num(gate.get("MAX_RATE_WAIT"), "MAX_RATE_WAIT"),
        status_interval=_num(reader.get("STATUS_INTERVAL"), "STATUS_INTERVAL"),
        reopen_attempts=_count(session.get("REOPEN_ATTEMPTS"), "REOPEN_ATTEMPTS"),
        max_reopens=_count(session.get("MAX_REOPENS_PER_SESSION"), "MAX_REOPENS_PER_SESSION"),
        hotkey=hotkey,
        commands=commands,
        cli_options_by_command=cli_options_by_command,
        refs=refs,
        cli_options=cli_options,
    )


def check_services(facts: SafetyFacts, services: dict[str, dict[int, str]]) -> None:
    """services.toml must describe exactly the services the source lists."""
    for group, actual in (("allowed", facts.allowed_services), ("never", facts.never_services)):
        described = set(services[group])
        missing = sorted(actual - described)
        extra = sorted(described - actual)
        if missing:
            raise BuildError(f"services.toml [{group}] has no description for {', '.join(f'0x{s:02X}' for s in missing)}")
        if extra:
            raise BuildError(f"services.toml [{group}] describes {', '.join(f'0x{s:02X}' for s in extra)}, which the source doesn't list")


def check_commands(facts: SafetyFacts, roadmap: Roadmap) -> None:
    """Roadmap commands must exist in cli.py, and every CLI command must appear under its phase."""
    listed: dict[str, set[int]] = {}
    for phase in roadmap.phases:
        for command in phase.commands:
            words = command.split()
            if len(words) < 2 or words[0] != "lasto" or words[1] not in facts.commands:
                raise BuildError(f"roadmap phase {phase.number} lists {command!r}, which cli.py doesn't have")
            if len(words) == 2:
                listed.setdefault(words[1], set()).add(phase.number)
    for name, (_help, phase) in facts.commands.items():
        if phase not in listed.get(name, set()):
            raise BuildError(f"cli.py says 'lasto {name}' arrives in Phase {phase}, but roadmap.toml doesn't list it there")
