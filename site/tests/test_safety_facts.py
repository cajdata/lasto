"""The site reads the safety core's allowlist from source. These tests pin that down.

Tests that change the app's source change a copy, and find what they change by name (source_edit),
so they keep working as the code they edit moves on.
"""

import ast
import shutil
from pathlib import Path

import pytest
import source_edit as se

from sitegen import data, paths, safety
from sitegen.data import BuildError

FILES = ["safety/policy.py", "safety/ecus.py", "safety/ratelimit.py", "safety/killswitch.py",
         "safety/interlocks.py", "safety/gate.py", "safety/hotkey.py", "safety/session.py", "safety/reader.py", "cli.py"]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A copy of just the app files the site reads, to edit safely in a test."""
    for rel in FILES:
        dst = tmp_path / "src" / "lasto" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(paths.APP / rel, dst)
    return tmp_path


# The service lists in CLAUDE.md's rule 3. The safety core may allow fewer (Phase 3 trims the
# allowlist to what the Creader shows) and refuse more, never the other way.
CLAUDE_MD_OBD = {0x01, 0x02, 0x03, 0x06, 0x07, 0x09, 0x0A}
CLAUDE_MD_MANUFACTURER = {0x13, 0x17, 0x18, 0x19, 0x1A, 0x21, 0x22}
CLAUDE_MD_NEVER = {
    0x04, 0x08, 0x10, 0x11, 0x14, 0x23, 0x27, 0x28, 0x2C, 0x2E, 0x2F, 0x30, 0x31,
    0x34, 0x35, 0x36, 0x37, 0x38, 0x3B, 0x3D, 0x3E, 0x85,
}


def test_the_service_lists_follow_the_rules_in_claude_md():
    f = safety.load_facts()
    assert f.obd_services <= CLAUDE_MD_OBD and f.manufacturer_services <= CLAUDE_MD_MANUFACTURER
    assert f.never_services >= CLAUDE_MD_NEVER
    assert not f.allowed_services & f.never_services


def test_other_facts_come_from_source():
    f = safety.load_facts()
    assert f.functional_id == 0x7DF
    # The engine computer, and any module a Creader capture confirms later: each with its own request and answer IDs.
    ecus = [(e.request_id, e.response_id) for e in f.ecus]
    assert (0x7E0, 0x7E8) in ecus
    assert len(set(ecus)) == len(ecus) and all(request != response for request, response in ecus)
    assert f.rates["logging"] == 20 and f.rates["discovery"] == 5 and f.ceiling == 20
    assert f.min_engine_off_voltage == 12.0
    assert f.hotkey == "Ctrl+Alt+K"


def test_writer_ceiling_and_passive_trust_come_from_source():
    f = safety.load_facts()
    # The Writer's own backstop: CEILING_FRAMES request frames in any CEILING_WINDOW seconds.
    assert f.ceiling_frames == 20 and f.ceiling_window == 1.0
    # The gate refuses a request whose rate slot is further away than this.
    assert f.max_rate_wait == 1.0
    # Passive capture re-reads listen-only on every status check, this often.
    assert f.status_interval == 0.1
    # A distrusted passive channel: attempts per incident, and reopens per session.
    assert f.reopen_attempts == 3 and f.max_reopens == 10


def test_read_only_wrappers_and_arithmetic_are_evaluated():
    env: dict[str, object] = {"A": 20.0, "B": 1.0}
    assert safety._evaluate(ast.parse("MappingProxyType({1: 2.0})", mode="eval").body, env) == {1: 2.0}
    assert safety._evaluate(ast.parse("int(A * B)", mode="eval").body, env) == 20
    assert safety._evaluate(ast.parse("A / 4 - 1", mode="eval").body, env) == 4.0


def test_counts_must_be_whole_numbers(tree):
    # With / allowed, a count can come out fractional. The site must refuse it, never round it down.
    se.rebind(tree, "safety/killswitch.py", "WINDOW_NRC_LIMIT", lambda v: f"({v}) + 0.5")
    with pytest.raises(BuildError, match="WINDOW_NRC_LIMIT"):
        safety.load_facts(tree)


def test_a_whole_number_float_is_a_count(tree):
    limit = safety.load_facts(tree).nrc_window_limit
    se.rebind(tree, "safety/killswitch.py", "WINDOW_NRC_LIMIT", lambda v: f"({v}) * 10 / 10")
    assert safety.load_facts(tree).nrc_window_limit == limit


def test_a_constant_computed_from_an_unstable_one_fails_the_build(tree):
    # The app computes CONSECUTIVE_NRC_LIMIT from whichever _BASE the branch left; the site can't know which.
    base = se.value_source(tree, "safety/killswitch.py", "CONSECUTIVE_NRC_LIMIT")
    se.insert_before(tree, "safety/killswitch.py", se.assignment("CONSECUTIVE_NRC_LIMIT"),
                     f"_BASE = {base}\nif __debug__:\n    _BASE = ({base}) + 2")
    se.rebind(tree, "safety/killswitch.py", "CONSECUTIVE_NRC_LIMIT", lambda v: "_BASE")
    with pytest.raises(BuildError, match="CONSECUTIVE_NRC_LIMIT"):
        safety.load_facts(tree)


def test_a_line_the_site_cant_evaluate_is_skipped_not_fatal(tmp_path):
    for source in ("-'a'", "-A.B", "frozenset({Ecu(name='a')})"):
        with pytest.raises(BuildError):
            safety._evaluate(ast.parse(source, mode="eval").body, {})
    p = tmp_path / "m.py"
    p.write_text("import math\nY = -math.inf\nZ = frozenset({Ecu(name='a')})\nX = 1\n", encoding="utf-8")
    assert safety._read(p, tmp_path).get("X") == 1


def test_arithmetic_errors_are_build_errors(tree):
    for source in ("1 / 0", "int(1e400)", "int(1e400 - 1e400)"):
        with pytest.raises(BuildError):
            safety._evaluate(ast.parse(source, mode="eval").body, {})
    se.rebind(tree, "safety/ratelimit.py", "CEILING_WINDOW", lambda v: "0.0")
    with pytest.raises(BuildError, match="CEILING_WINDOW"):
        safety.load_facts(tree)


def test_powers_are_evaluated_within_bounds():
    assert safety._evaluate(ast.parse("2**30", mode="eval").body, {}) == 2**30
    for source in ("2**1000", "2**-1", "2**0.5", "'a'**2", "1e308**2", "(1e200**2)**2", "(2**64)**64", "A * A"):
        with pytest.raises(BuildError):
            safety._evaluate(ast.parse(source, mode="eval").body, {"A": 2**4000})  # whole numbers past 4096 bits


def test_cli_options_come_from_source():
    f = safety.load_facts()
    assert {"--live", "--channel", "--port", "--data", "--profile", "--seconds", "--bus", "--id", "--from", "--to"} <= f.cli_options
    assert all(o.startswith("--") for o in f.cli_options)
    by = f.cli_options_by_command
    assert by["drive"] == {"--help", "--live", "--channel", "--port", "--data", "--profile", "--seconds"}
    assert by["log"] == {"--help", "--data", "--bus", "--id", "--from", "--to"}
    assert by["map"] == {"--help", "--live", "--channel", "--port"}
    assert by["gui"] == {"--help"}
    assert by[""] == {"--help", "--version"}  # bare `lasto`
    assert set(by) == set(f.commands) | {""}


# Parts of cli.py's build_parser, found by structure.
LOG_BLOCK = se.within("build_parser", se.if_testing('name == "log"'), 'if name == "log"')
BUILT_BLOCK = se.within("build_parser", se.if_testing("name in BUILT"), "if name in BUILT")
PROFILE_CALL = se.within("build_parser", se.call_with_first_arg("add_argument", "--profile"), "the --profile add_argument")
PROFILE = se.within("build_parser", se.statement_of(se.call_with_first_arg("add_argument", "--profile")), "the --profile statement")
ADD_PARSER = se.within("build_parser", lambda n: isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and n.func.attr == "add_parser", "the add_parser call")
LOOP = se.within("build_parser", lambda n: isinstance(n, ast.For), "the loop over COMMANDS")

SHAPES = {
    "a test it can't follow": lambda t: se.replace(t, "cli.py", lambda tree: LOG_BLOCK(tree).test, lambda s: 'name.startswith("log")'),
    "an else branch": lambda t: se.replace(t, "cli.py", LOG_BLOCK, lambda s: "el" + s),
    "control flow": lambda t: se.insert_before(t, "cli.py", BUILT_BLOCK, 'if name == "gui":\n    continue'),
    "not a bare call": lambda t: se.replace(t, "cli.py", PROFILE, lambda s: f"action = {s}"),
    "a nested block": lambda t: se.nest(t, "cli.py", PROFILE, "if True:"),
    "another parser": lambda t: se.replace(t, "cli.py", lambda tree: PROFILE_CALL(tree).func.value, lambda s: "parser"),
    "a conditional expression": lambda t: se.replace(t, "cli.py", PROFILE_CALL, lambda s: f"{s} if name else None"),
    "not a command": lambda t: se.rebind(t, "cli.py", "BUILT", lambda v: f'{v} | frozenset({{"fly"}})'),
    "parents=": lambda t: se.replace(t, "cli.py", ADD_PARSER, se.with_keyword("parents=[]")),
    "add_help=": lambda t: se.replace(t, "cli.py", ADD_PARSER, se.with_keyword("add_help=False")),
    "aliases=": lambda t: se.replace(t, "cli.py", ADD_PARSER, se.with_keyword('aliases=["d"]')),
    "a helper, not the subparsers": lambda t: se.replace(t, "cli.py", ADD_PARSER,
                                                         lambda s: "add_parser(subparsers, " + s.split("(", 1)[1]),
    "for ... else": lambda t: se.insert_after(t, "cli.py", LOOP, 'else:\n    parser.add_argument("--x")'),
    "build_parser twice": lambda t: se.append(t, "cli.py", "def build_parser():\n    return None"),
}


@pytest.mark.parametrize("shape", SHAPES)
def test_cli_options_need_a_shape_the_site_understands(tree, shape):
    # Each of these would make the site credit an option to the wrong command, so it refuses them.
    safety.load_facts(tree)  # the copy reads fine before the edit
    SHAPES[shape](tree)
    with pytest.raises(BuildError, match="cli.py"):
        safety.load_facts(tree)


def test_cli_without_options_fails_the_build(tree):
    p = tree / "src" / "lasto" / "cli.py"
    p.write_text(p.read_text(encoding="utf-8").replace(".add_argument(", ".add_arg("), encoding="utf-8")
    with pytest.raises(BuildError, match="no --options"):
        safety.load_facts(tree)


@pytest.mark.parametrize("rebind", [
    "import os as X",
    "from os import sep as X",
    "def X():\n    pass",
    "class X:\n    pass",
    "(X := 2)",
    "Y = [z for z in range(3) if (X := z)]",
    "del X",
    "try:\n    pass\nexcept ValueError as X:\n    pass",
    "match 1:\n    case X:\n        pass",
    "from os import *",
    # := in the parts of a def, class, or lambda that run in the module's scope
    "def f(a=(X := 2)):\n    pass",
    "@(X := staticmethod)\ndef f():\n    pass",
    "class C((X := object)):\n    pass",
    "Y = lambda a=(X := 2): a",
    # changing a constant's contents in place
    "X[0] = 2",
    "del X[0]",
    "X.update({0: 2})",
    "Y = [(A := (X := z)) for z in range(3)]",
    "(A := X.update({0: 2}))",
    "class C:\n    X.update({0: 2})",
    "def f():\n    X[0] = 2\nf()",
    "globals()['X'] = 2",
    "globals().update(X=2)",
])
def test_every_way_of_rebinding_a_constant_is_refused(tmp_path, rebind):
    p = tmp_path / "m.py"
    p.write_text(f"X = 1\n{rebind}\n", encoding="utf-8")
    with pytest.raises(BuildError, match="changes X"):
        safety._read(p, tmp_path).get("X")


@pytest.mark.parametrize("local", [
    "def f():\n    X = 2\n    return X",
    "class C:\n    X = 2",
    "Y = [X for X in range(3)]",
    "Y = lambda X: X",
    "def f(X):\n    X[0] = 2",
    "def f():\n    X = {}\n    X[0] = 2",
])
def test_names_bound_in_their_own_scope_dont_count(tmp_path, local):
    p = tmp_path / "m.py"
    p.write_text(f"X = 1\n{local}\n", encoding="utf-8")
    assert safety._read(p, tmp_path).get("X") == 1


def test_other_calls_are_never_evaluated():
    for source in ("open('x')", "int(open('x'))", "MappingProxyType(globals())", "__import__('os')"):
        with pytest.raises(BuildError):
            safety._evaluate(ast.parse(source, mode="eval").body, {})


def test_services_file_agrees_with_source():
    safety.check_services(safety.load_facts(), data.load_services())


def test_each_phase_lists_its_commands_from_cli_py():
    f, r = safety.load_facts(), data.load_roadmap()
    by_phase = safety.phase_commands(f, r)
    bare = [c for commands in by_phase.values() for c in commands if len(c.split()) == 2]
    assert sorted(bare) == sorted(f"lasto {name}" for name in f.commands)  # every command, under one phase
    for p in r.phases:  # the phase's own, in cli.py's order, then roadmap.toml's extras
        own = [f"lasto {name}" for name, (_help, phase) in f.commands.items() if phase == p.number]
        assert by_phase[p.number] == (*own, *p.extra_commands)


def _add_command(tree: Path, name: str, phase: int) -> None:
    se.rebind(tree, "cli.py", "COMMANDS",
              lambda v: v.rstrip()[:-1].rstrip().rstrip(",") + f',\n    "{name}": ("Something new", {phase}),\n}}')


def test_a_new_cli_command_shows_under_its_phase(tree):
    # How Phase 3's `lasto adapter` reaches the roadmap: from cli.py, with no edit to roadmap.toml.
    _add_command(tree, "tune", 5)
    assert "lasto tune" in safety.phase_commands(safety.load_facts(tree), data.load_roadmap())[5]


def test_a_command_for_a_phase_the_roadmap_lacks_fails_the_build(tree):
    _add_command(tree, "tune", 12)
    with pytest.raises(BuildError, match="Phase 12"):
        safety.phase_commands(safety.load_facts(tree), data.load_roadmap())


@pytest.mark.parametrize("extra, match", [
    ("lasto fly --live", "names no command in cli.py"),
    ("lasto drive", "comes from cli.py"),  # bare commands are listed from cli.py, never by hand
    ("git status", "isn't a lasto command"),
])
def test_a_roadmap_extra_must_name_a_real_command(extra, match):
    import dataclasses

    r = data.load_roadmap()
    r.phases[5] = dataclasses.replace(r.phases[5], extra_commands=(extra,))
    with pytest.raises(BuildError, match=match):
        safety.phase_commands(safety.load_facts(), r)


def test_a_new_allowed_service_changes_the_facts(tree):
    se.rebind(tree, "safety/policy.py", "MANUFACTURER_READ_SERVICES", lambda v: f"{v} | frozenset({{0x1C}})")
    f = safety.load_facts(tree)
    assert 0x1C in f.allowed_services
    with pytest.raises(BuildError, match="no description for 0x1C"):
        safety.check_services(f, data.load_services())


def test_a_trimmed_allowlist_needs_no_edit_to_services_toml(tree):
    # Phase 3 trims the manufacturer allowlist to what the Creader shows; the site follows with no data edit.
    kept = sorted(safety.load_facts(tree).manufacturer_services)[:-1]
    se.rebind(tree, "safety/policy.py", "MANUFACTURER_READ_SERVICES",
              lambda v: "frozenset({" + ", ".join(f"0x{s:02X}" for s in kept) + "})")
    f = safety.load_facts(tree)
    assert f.manufacturer_services == set(kept)
    safety.check_services(f, data.load_services())  # a spare description is fine; a missing one isn't


def test_a_service_both_allowed_and_never_fails_the_build(tree):
    se.rebind(tree, "safety/policy.py", "MANUFACTURER_READ_SERVICES", lambda v: f"{v} | frozenset({{0x2E}})")
    with pytest.raises(BuildError, match="both allowed and never"):
        safety.load_facts(tree)


def test_a_constant_that_stops_being_literal_fails_the_build(tree):
    se.rebind(tree, "safety/policy.py", "NEVER_SERVICES", lambda v: f"compute_never({v})")
    with pytest.raises(BuildError, match="NEVER_SERVICES"):
        safety.load_facts(tree)


def test_source_is_never_executed(tree):
    # A side effect at import time would create this file. Parsing must not run it.
    marker = tree / "ran.txt"
    se.insert_after(tree, "safety/policy.py", se.assignment("FUNCTIONAL_REQUEST_ID"), f"open({str(marker)!r}, 'w').write('x')")
    safety.load_facts(tree)
    assert not marker.exists()


def test_rate_above_the_ceiling_fails_the_build(tree):
    def logging_rate(tree_: ast.Module) -> ast.AST:
        rates = se.value("PURPOSE_RATES")(tree_)
        return se.one((v for n in ast.walk(rates) if isinstance(n, ast.Dict)
                       for k, v in zip(n.keys, n.values, strict=True) if isinstance(k, ast.Attribute) and k.attr == "LOGGING"),
                      "the LOGGING rate")

    se.replace(tree, "safety/ratelimit.py", logging_rate, lambda v: "HARD_CEILING_PER_SECOND + 5")
    with pytest.raises(BuildError, match="above the hard ceiling"):
        safety.load_facts(tree)


def test_site_never_imports_the_app():
    for py in paths.SITE.rglob("*.py"):
        if "dist" in py.parts or ".cache" in py.parts:
            continue
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert name != "lasto" and not name.startswith("lasto."), f"{py} imports {name}"


def test_hotkey_comes_from_the_register_call(tree):
    key = safety.load_facts(tree).hotkey.rsplit("+", 1)[1]

    def modifiers(t: ast.Module) -> ast.AST:  # the RegisterHotKey call's third argument
        return se.one((n.args[2] for n in ast.walk(t) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and n.func.attr == "RegisterHotKey"), "RegisterHotKey call")

    se.replace(tree, "safety/hotkey.py", modifiers, lambda s: "MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT")
    se.insert_after(tree, "safety/hotkey.py", se.assignment("MOD_ALT"), "MOD_SHIFT = 0x0004")
    assert safety.load_facts(tree).hotkey == f"Ctrl+Shift+{key}"


def test_augmented_constant_fails_the_build(tree):
    se.append(tree, "safety/policy.py", "NEVER_SERVICES |= {0x99}")
    with pytest.raises(BuildError, match="changes NEVER_SERVICES"):
        safety.load_facts(tree)


def test_conditionally_redefined_constant_fails_the_build(tree):
    se.append(tree, "safety/policy.py", "if FUNCTIONAL_REQUEST_ID:\n    OBD_SERVICES = frozenset({0x01})")
    with pytest.raises(BuildError, match="changes OBD_SERVICES"):
        safety.load_facts(tree)


def test_listen_window_and_response_pending_come_from_source():
    f = safety.load_facts()
    assert f.listen_window == 2.0
    assert f.response_pending_max == 10 and f.response_pending_wait == 5.0
