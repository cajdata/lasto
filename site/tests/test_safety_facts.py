"""The site reads the safety core's allowlist from source. These tests pin that down."""

import ast
import shutil
from pathlib import Path

import pytest

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


def edit(tree: Path, rel: str, old: str, new: str) -> None:
    p = tree / "src" / "lasto" / rel
    text = p.read_text(encoding="utf-8")
    assert old in text, f"test setup: {old!r} not in {rel}"
    p.write_text(text.replace(old, new), encoding="utf-8")


def test_real_source_matches_the_rules_in_claude_md():
    f = safety.load_facts()
    assert f.obd_services == {0x01, 0x02, 0x03, 0x06, 0x07, 0x09, 0x0A}
    assert f.manufacturer_services == {0x13, 0x17, 0x18, 0x19, 0x1A, 0x21, 0x22}
    assert f.never_services == {
        0x04, 0x08, 0x10, 0x11, 0x14, 0x23, 0x27, 0x28, 0x2C, 0x2E, 0x2F, 0x30, 0x31,
        0x34, 0x35, 0x36, 0x37, 0x38, 0x3B, 0x3D, 0x3E, 0x85,
    }
    assert not f.allowed_services & f.never_services
    assert f.functional_id == 0x7DF
    assert [(e.request_id, e.response_id) for e in f.ecus] == [(0x7E0, 0x7E8)]
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
    edit(tree, "safety/killswitch.py", "WINDOW_NRC_LIMIT = 10", "WINDOW_NRC_LIMIT = CONSECUTIVE_NRC_LIMIT * 3.5")
    with pytest.raises(BuildError, match="WINDOW_NRC_LIMIT"):
        safety.load_facts(tree)


def test_a_whole_number_float_is_a_count(tree):
    edit(tree, "safety/killswitch.py", "WINDOW_NRC_LIMIT = 10", "WINDOW_NRC_LIMIT = CONSECUTIVE_NRC_LIMIT * 10 / 3")
    assert safety.load_facts(tree).nrc_window_limit == 10


def test_a_constant_computed_from_an_unstable_one_fails_the_build(tree):
    # The app computes CONSECUTIVE_NRC_LIMIT from whichever _BASE the branch left; the site can't know which.
    edit(tree, "safety/killswitch.py", "CONSECUTIVE_NRC_LIMIT = 3",
         "_BASE = 3\nif __debug__:\n    _BASE = 5\nCONSECUTIVE_NRC_LIMIT = _BASE")
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
    edit(tree, "safety/ratelimit.py", "CEILING_WINDOW = 1.0", "CEILING_WINDOW = 0.0")
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


PROFILE = 'sub.add_argument("--profile", help="polled logging profile (default: passive capture)")'


@pytest.mark.parametrize("old, new", [
    ('if name == "log":', 'if name.startswith("log"):'),  # a test it can't follow
    ('if name == "log":', 'elif name == "log":'),  # an else branch
    ("if name in BUILT:", 'if name == "gui":\n            continue\n        if name in BUILT:'),  # control flow
    (PROFILE, f"action = {PROFILE}"),  # not a bare call
    (PROFILE, f"if True:\n                {PROFILE}"),  # a nested block
    (PROFILE, PROFILE.replace("sub.", "parser.")),  # another parser
    (PROFILE, f"{PROFILE} if name else None"),  # a conditional expression
    ('BUILT = frozenset({"drive", "log"})', 'BUILT = frozenset({"drive", "log", "fly"})'),  # not a command
    ("allow_abbrev=False)\n        if name in HARDWARE", "allow_abbrev=False, parents=[])\n        if name in HARDWARE"),
    ("allow_abbrev=False)\n        if name in HARDWARE", "allow_abbrev=False, add_help=False)\n        if name in HARDWARE"),
    ("allow_abbrev=False)\n        if name in HARDWARE", 'allow_abbrev=False, aliases=["d"])\n        if name in HARDWARE'),
    ("sub = subparsers.add_parser(name,", "sub = add_parser(subparsers, name,"),  # a helper, not the subparsers
    ("    return parser\n", '    else:\n        parser.add_argument("--x")\n    return parser\n'),  # for ... else
    ("def _check_hardware_arguments", "def build_parser():\n    return None\n\n\ndef _check_hardware_arguments"),
])
def test_cli_options_need_a_shape_the_site_understands(tree, old, new):
    # Each of these would make the site credit an option to the wrong command, so it refuses them.
    edit(tree, "cli.py", old, new)
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


def test_services_file_and_roadmap_agree_with_source():
    f = safety.load_facts()
    safety.check_services(f, data.load_services())
    safety.check_commands(f, data.load_roadmap())


def test_a_new_allowed_service_changes_the_facts(tree):
    edit(tree, "safety/policy.py", "0x21, 0x22})", "0x21, 0x22, 0x1C})")
    f = safety.load_facts(tree)
    assert 0x1C in f.allowed_services
    with pytest.raises(BuildError, match="no description for 0x1C"):
        safety.check_services(f, data.load_services())


def test_a_service_both_allowed_and_never_fails_the_build(tree):
    edit(tree, "safety/policy.py", "0x21, 0x22})", "0x21, 0x22, 0x2E})")
    with pytest.raises(BuildError, match="both allowed and never"):
        safety.load_facts(tree)


def test_a_constant_that_stops_being_literal_fails_the_build(tree):
    edit(tree, "safety/policy.py", "NEVER_SERVICES = frozenset(", "NEVER_SERVICES = compute_never(")
    with pytest.raises(BuildError, match="NEVER_SERVICES"):
        safety.load_facts(tree)


def test_source_is_never_executed(tree):
    # A side effect at import time would create this file. Parsing must not run it.
    marker = tree / "ran.txt"
    edit(tree, "safety/policy.py", "FUNCTIONAL_REQUEST_ID = 0x7DF",
         f"FUNCTIONAL_REQUEST_ID = 0x7DF\nopen({str(marker)!r}, 'w').write('x')")
    safety.load_facts(tree)
    assert not marker.exists()


def test_rate_above_the_ceiling_fails_the_build(tree):
    edit(tree, "safety/ratelimit.py", "Purpose.LOGGING: 20.0", "Purpose.LOGGING: 25.0")
    with pytest.raises(BuildError, match="above the hard ceiling"):
        safety.load_facts(tree)


def test_roadmap_must_list_every_cli_command(tree):
    edit(tree, "cli.py", '"verify": ("Confirm solver candidates as verified definitions", 3),',
         '"verify": ("Confirm solver candidates as verified definitions", 3),\n    "tune": ("Something new", 5),')
    f = safety.load_facts(tree)
    with pytest.raises(BuildError, match="lasto tune"):
        safety.check_commands(f, data.load_roadmap())


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
    edit(tree, "safety/hotkey.py", "MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_K", "MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_K")
    edit(tree, "safety/hotkey.py", "MOD_ALT = 0x0001", "MOD_ALT = 0x0001\nMOD_SHIFT = 0x0004")
    assert safety.load_facts(tree).hotkey == "Ctrl+Shift+K"


def test_augmented_constant_fails_the_build(tree):
    edit(tree, "safety/policy.py", "PROBE_PIDS = frozenset({0x0C, 0x0D, 0x42})",
         "PROBE_PIDS = frozenset({0x0C, 0x0D, 0x42})\nNEVER_SERVICES |= {0x99}")
    with pytest.raises(BuildError, match="changes NEVER_SERVICES"):
        safety.load_facts(tree)


def test_conditionally_redefined_constant_fails_the_build(tree):
    edit(tree, "safety/policy.py", "PROBE_PIDS = frozenset({0x0C, 0x0D, 0x42})",
         "PROBE_PIDS = frozenset({0x0C, 0x0D, 0x42})\nif FUNCTIONAL_REQUEST_ID:\n    OBD_SERVICES = frozenset({0x01})")
    with pytest.raises(BuildError, match="changes OBD_SERVICES"):
        safety.load_facts(tree)


def test_listen_window_and_response_pending_come_from_source():
    f = safety.load_facts()
    assert f.listen_window == 2.0
    assert f.response_pending_max == 10 and f.response_pending_wait == 5.0
