"""Facts about the safety core and CLI, read from their source with ast.

The build never imports lasto. It parses the source files and evaluates only literal
constants (numbers, strings, sets, dicts, frozenset(...), and | between known sets), so
nothing here can execute app code or reach hardware. Every number the site shows about
what Lasto may send comes from here, so the site can't drift from the code.
"""

from __future__ import annotations

import ast
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


def _evaluate(node: ast.AST, env: dict[str, object]) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_evaluate(node.operand, env)  # type: ignore[operator]
    if isinstance(node, ast.Set):
        return {_evaluate(e, env) for e in node.elts}
    if isinstance(node, ast.Tuple):
        return tuple(_evaluate(e, env) for e in node.elts)
    if isinstance(node, ast.List):
        return [_evaluate(e, env) for e in node.elts]
    if isinstance(node, ast.Dict):
        if any(k is None for k in node.keys):
            raise BuildError("dict ** unpacking isn't a literal")
        return {_evaluate(k, env): _evaluate(v, env) for k, v in zip(node.keys, node.values, strict=True)}
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
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == "frozenset" and len(node.args) == 1 and not node.keywords:
            return frozenset(_evaluate(node.args[0], env))  # type: ignore[arg-type]
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
    # Names bound anywhere other than one top-level assignment: reassigned, augmented (|=),
    # bound inside a function, class, loop, or branch, or declared global. Their top-level
    # value may not be the one the app uses, so the site refuses to read them.
    unstable: set[str] = field(default_factory=set)

    def get(self, name: str) -> object:
        if name in self.unstable:
            raise BuildError(f"{self.rel} changes {name} after defining it; the site can only read single literal constants")
        if name not in self.values:
            raise BuildError(f"{self.rel} no longer defines {name} as a literal constant; update sitegen/safety.py")
        return self.values[name]

    @property
    def rel(self) -> str:
        return self.path.relative_to(self.root).as_posix()


def _read(path: Path, root: Path) -> _Module:
    if not path.exists():
        raise BuildError(f"can't find {path}")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    mod = _Module(path, root, tree)
    top_level: dict[str, int] = {}
    for stmt in tree.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        top_level[n.id] = top_level.get(n.id, 0) + 1
    mod.unstable = {name for name, count in top_level.items() if count > 1}
    for node in ast.walk(tree):
        # x |= ..., anywhere, or a global/nonlocal declaration that lets a function rebind a constant.
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            mod.unstable.add(node.target.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            mod.unstable.update(node.names)
    # Names bound inside module-level if/try/for/while/with/match blocks.
    for stmt in tree.body:
        if isinstance(stmt, (ast.If, ast.Try, ast.For, ast.While, ast.With, ast.Match)):
            for n in ast.walk(stmt):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    mod.unstable.add(n.id)
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            name, value = stmt.targets[0].id, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            name, value = stmt.target.id, stmt.value
        else:
            continue
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
    hotkey: str
    commands: dict[str, tuple[str, int]]
    refs: dict[str, SourceRef]

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

    hotkey = _hotkey(hot)

    raw_commands = cli.get("COMMANDS")
    if not isinstance(raw_commands, dict):
        raise BuildError("cli.py COMMANDS isn't a dict")
    commands = {str(k): (str(v[0]), int(v[1])) for k, v in raw_commands.items()}

    refs: dict[str, SourceRef] = {}
    for mod in (policy, ecus, rate, kill, inter, gate, hot, session, cli):
        for name, ref in mod.refs.items():
            refs.setdefault(name, ref)

    return SafetyFacts(
        obd_services=obd,
        manufacturer_services=mfr,
        never_services=never,
        dtc_read_services=_ints(policy.get("DTC_READ_SERVICES"), "DTC_READ_SERVICES"),
        probe_pids=_ints(policy.get("PROBE_PIDS"), "PROBE_PIDS"),
        functional_id=int(_num(policy.get("FUNCTIONAL_REQUEST_ID"), "FUNCTIONAL_REQUEST_ID")),
        padding_byte=int(_num(policy.get("PADDING_BYTE"), "PADDING_BYTE")),
        ecus=ecu_list,
        rates=rates,
        ceiling=ceiling,
        backoff_start=_num(rate.get("BACKOFF_START"), "BACKOFF_START"),
        backoff_max=_num(rate.get("BACKOFF_MAX"), "BACKOFF_MAX"),
        nrc_consecutive=int(_num(kill.get("CONSECUTIVE_NRC_LIMIT"), "CONSECUTIVE_NRC_LIMIT")),
        nrc_window_limit=int(_num(kill.get("WINDOW_NRC_LIMIT"), "WINDOW_NRC_LIMIT")),
        nrc_window_seconds=_num(kill.get("NRC_WINDOW_SECONDS"), "NRC_WINDOW_SECONDS"),
        timeout_limit=int(_num(kill.get("CONSECUTIVE_TIMEOUT_LIMIT"), "CONSECUTIVE_TIMEOUT_LIMIT")),
        min_engine_off_voltage=_num(inter.get("MIN_ENGINE_OFF_VOLTAGE"), "MIN_ENGINE_OFF_VOLTAGE"),
        response_pending_wait=_num(gate.get("P2_STAR_TIMEOUT"), "P2_STAR_TIMEOUT"),
        response_pending_max=int(_num(gate.get("MAX_RESPONSE_PENDING"), "MAX_RESPONSE_PENDING")),
        listen_window=_num(session.get("LISTEN_WINDOW"), "LISTEN_WINDOW"),
        hotkey=hotkey,
        commands=commands,
        refs=refs,
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
