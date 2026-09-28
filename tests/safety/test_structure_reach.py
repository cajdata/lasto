"""Findings #1 (and the static half of #2): nothing reaches around the safety core.

- Code outside the safety core may only use the safety core's public API.
  Names are resolved through every re-export and attribute chain, so
  `from lasto.safety.session import open_active` counts as using
  lasto.safety.pcan_active.open_active.
- No code anywhere in src/ uses a deliberate route around the core's guards
  (ctypes, gc, inspect, importlib, builtins, pickle and friends, sys.modules,
  vars/globals/setattr/delattr, getattr with a private or computed name,
  attribute-guard dunders, function defaults, frames, trace and import hooks,
  an explicit __init__ call other than super().__init__() (finding P1),
  an assignment or deletion inside an imported module, such as
  time.monotonic = f or os.environ[key] = value (finding P3),
  or, outside the safety core, another object's private attributes), except
  the exemptions listed below. Adding an exemption is its own commit,
  approved by the owner.
- Nothing in src/ or the tests changes a safety module, class, or
  module-level object: no assignment, deletion, in-place change, setattr,
  monkeypatch, or mock.patch aimed at one, however it is imported. Tests may
  change instances they made. test_frozen.py shows the same changes refused
  at runtime.
"""

from __future__ import annotations

import ast

import pytest
from scan import changes_to_the_safety_core, deliberate_routes, in_safety, sources, sources_of_tests, uses

PUBLIC_API = {
    "lasto.safety.session": {"open_passive_session", "open_polled_session", "PassiveSession", "PolledSession"},
    "lasto.safety.requests": {
        "Purpose", "DtcKind", "Request", "read_pid", "read_freeze_frame", "read_dtcs", "read_mode06",
        "read_vehicle_info", "read_local_id", "read_did", "read_ecu_id", "read_dtcs_kwp", "read_dtc_status",
        "read_dtcs_by_status", "read_dtc_information", "interlock_probe",
    },
    "lasto.safety.ecus": {"APPROVED_ECUS", "ENGINE", "Ecu", "EcuKind"},
    "lasto.safety.errors": {
        "SafetyError", "SafetyViolation", "KillSwitchTripped", "PassiveModeUnconfirmed", "InterfaceError", "AdapterError",
    },
    "lasto.safety.frames": {"CanFrame", "ErrorFrame", "StatusMessage", "ReadError", "Received"},
    "lasto.safety.audit": {"Auditor", "AuditSink", "JsonlAuditSink", "MemoryAuditSink"},
    "lasto.safety.clock": {"Clock", "SystemClock"},
    "lasto.safety.exchange": {"Exchange", "ExchangeState"},
    "lasto.safety.stn_port": {"StnAdapter", "open_adapter"},
}  # fmt: skip

# The simulator stands in for PCANBasic.dll, so it speaks the DLL's constants and structures.
SIMULATOR_ALSO_USES = {"lasto.safety.pcan_constants"}
# The test plugin clears the process kill switch before each test (finding C). Nothing else may.
PLUGIN_ALSO_USES = {("lasto.sim.pytest_plugin", ("name", "lasto.safety.killswitch", "reset_for_tests"))}

# Every allowed deliberate route, with its reason. Keep this minimal; changes need the owner's approval.
EXEMPTIONS = {
    ("ctypes", "lasto.safety.pcan_constants"): "PCAN-Basic message and timestamp structures",
    ("ctypes", "lasto.safety.pcan_dll"): "the read-only binding of PCANBasic.dll",
    ("ctypes", "lasto.safety.pcan_active"): "the one CAN_Write binding and its message buffer",
    ("ctypes", "lasto.safety.hotkey"): "user32/kernel32 calls for the Ctrl+Alt+K kill-switch hotkey",
    ("ctypes", "lasto.sim.pytest_plugin"): "the test hardware firewall wraps ctypes.CDLL.__init__",
    ("sys.modules", "lasto.sim.pytest_plugin"): "the test hardware firewall replaces pyserial with a stub",
    ("change ctypes.CDLL.__init__", "lasto.sim.pytest_plugin"): "the test hardware firewall wraps ctypes.CDLL.__init__",
    # The freezing helper (finding #2): what it takes to make the rest of the core unchangeable.
    ("sys.modules", "lasto.safety._frozen"): "find the module being frozen, and each submodule its package may bind",
    ("vars", "lasto.safety._frozen"): "read a module's names, to seal the classes it defines and see what is bound",
    ("__class__", "lasto.safety._frozen"): "turn each module into a FrozenModule, and make SealedType its own metaclass",
    ("__setattr__", "lasto.safety._frozen"): "the guards pass a change through until the class is sealed",
    ("__delattr__", "lasto.safety._frozen"): "the guards pass a deletion through until the class is sealed",
}


def allowed(target: tuple[str, ...], user: str) -> bool:
    if user.startswith("lasto.sim") and target[1] in SIMULATOR_ALSO_USES:
        return True
    if (user, target) in PLUGIN_ALSO_USES:
        return True
    if target[0] == "module":
        return target[1] == "lasto.safety" or target[1] in PUBLIC_API
    _, module, name = target
    return name in PUBLIC_API.get(module, set())


def violations(user: str, tree: ast.AST) -> list[str]:
    return sorted(".".join(t[1:]) for t in uses(tree) if in_safety(t) and not allowed(t, user))


def test_code_outside_the_safety_core_uses_only_its_public_api():
    outside = {name: tree for name, tree in sources().items() if not name.startswith("lasto.safety")}
    assert "lasto.cli" in outside and "lasto.sim.fake_pcan" in outside
    found = {name: violations(name, tree) for name, tree in outside.items()}
    assert {name: bad for name, bad in found.items() if bad} == {}


@pytest.mark.parametrize(
    ("snippet", "flagged"),
    [
        ("from lasto.safety.session import open_active", True),
        ("import lasto.safety.session as s\ns.open_active('PCAN_USBBUS1')", True),
        ("from lasto.safety import session\nsession.ActiveChannel", True),
        ("import lasto.safety.pcan_active", True),
        ("import lasto\nlasto.safety.pcan_active.open_active", True),
        ("from lasto.safety import gate\ngate.Gate", True),
        ("from lasto.safety.gate import Gate", True),
        ("from lasto.safety.audit import REFUSALS", True),
        ("from lasto.safety.killswitch import KillSwitch", True),
        ("from lasto.safety import pcan_dll as p\np.load_readonly()", True),
        ("def f():\n    from lasto.safety.pcan_active import open_active\n    return open_active", True),
        ("from lasto.safety.session import open_polled_session, open_passive_session", False),
        ("import lasto.safety.requests as rq\nrq.read_pid([12], purpose=rq.Purpose.LOGGING)", False),
        ("from lasto.safety.exchange import ExchangeState", False),
        ("from lasto.safety import errors\nerrors.SafetyViolation", False),
    ],
)
def test_the_scanner_follows_reexports_and_attribute_chains(snippet, flagged):
    assert bool(violations("lasto.probe", ast.parse(snippet))) is flagged


@pytest.mark.parametrize(
    "snippet",
    [
        "import ctypes",
        "from ctypes import windll",
        "import gc\ngc.get_referents(x)",
        "import inspect",
        "import importlib\nimportlib.import_module('lasto.safety.pcan_active')",
        "import sys\nsys.modules['lasto.safety.policy']",
        "import sys as s\ns.modules",
        "from sys import modules",
        "vars(policy)",
        "globals()['x'] = 1",
        "setattr(policy, 'NEVER_SERVICES', frozenset())",
        "delattr(policy, 'NEVER_SERVICES')",
        "getattr(session.reader, '_channel')",
        "getattr(session, name)",
        "hasattr(x, '_gate')",
        "policy.__dict__['NEVER_SERVICES'] = frozenset()",
        "object.__setattr__(request, 'payload', b'\\x04')",
        "object.__new__(Request)",
        "fn.__closure__[0].cell_contents",
        "session.reader._channel.write(1, b'')",
        "__import__('lasto.safety.pcan_active')",
        "import builtins\nbuiltins.isinstance = lambda *args: True",
        "import pickle",
        "from marshal import loads",
        "open_passive_session.__kwdefaults__['library'] = fake",
        "check_frame.__defaults__ = ()",
        "error.__traceback__.tb_frame.f_globals['policy'] = None",
        "frame.f_locals['data'] = b''",
        "import sys\nsys.settrace(tracer)",
        "import sys\nsys.meta_path.insert(0, finder)",
        "().__class__.__base__.__subclasses__()",
        # Finding P1: running __init__ again on something that exists rewrites it in place.
        "from lasto.safety.ecus import ENGINE\nENGINE.__init__('engine', kind, 0x0B0, 0x7E8, None, '')",
        "session.reader.__init__(channel, clock)",
        "type(request).__init__(request, target, payload, purpose)",
        "super(Gate, gate).__init__()",
        # Finding P3: changing what an imported module holds steers every user of it, the safety core included.
        "import time\ntime.monotonic = lambda: 0.0",
        "import time as clock_source\nclock_source.sleep = lambda seconds: None",
        "import time\ndel time.sleep",
        "import time\ntime.monotonic += 1.0",
        "import time\nfor time.monotonic in clocks:\n    pass",
        "import threading\nthreading.RLock = FakeLock",
        "import weakref\nweakref.WeakKeyDictionary.get = lambda *args: None",
        "from datetime import datetime\ndatetime.now = frozen",
        "import os\nos.environ['SystemRoot'] = 'D:\\\\elsewhere'",
    ],
)
def test_every_deliberate_route_is_caught(snippet):
    assert deliberate_routes("lasto.probe", ast.parse(snippet))


def test_sys_modules_is_reported_once_as_its_own_route():
    assert [route for route, _ in deliberate_routes("lasto.probe", ast.parse("import sys\nsys.modules['serial'] = stub"))] == [
        "sys.modules"
    ]


def test_ordinary_code_is_not_flagged():
    snippet = (
        "getattr(args, 'live', False)\nhasattr(value, 'value')\nself._private = 1\ncls._table\ntype(x).__name__\n"
        "import sys\nsys.argv\nsys.exit(1)\nimport json\njson.dumps(record)\nimport types\ntypes.MappingProxyType({})\n"
        "class Child(Parent):\n    def __init__(self):\n        super().__init__()\n"
        "self.clock = clock\nrecord['event'] = name\nimport os\nroot = os.environ.get('SystemRoot')\nimport time\nnow = time.monotonic()"
    )
    assert deliberate_routes("lasto.probe", ast.parse(snippet)) == []


# ---- nothing changes the safety core (finding #2) ----


@pytest.mark.parametrize(
    "snippet",
    [
        "from lasto.safety import policy\npolicy.NEVER_SERVICES = frozenset()",
        "import lasto.safety.policy as p\ndel p.NEVER_SERVICES",
        "import lasto\nlasto.safety.policy.NEVER_SERVICES = frozenset()",
        "import lasto\nlasto.safety = None",
        "from lasto.safety import ecus\necus.APPROVED_ECUS += (forged,)",
        "from lasto.safety.ratelimit import PURPOSE_RATES\nPURPOSE_RATES[purpose] = 1000.0",
        "from lasto.safety import ratelimit\nratelimit.PURPOSE_RATES.update({})",
        "from lasto.safety.audit import REFUSALS\nREFUSALS._backlog = None",
        "from lasto.safety.gate import Gate\nGate._transmit = lambda *args, **kwargs: None",
        "from lasto.safety.ecus import EcuKind\nEcuKind.SRS._name_ = 'ENGINE'",
        "from lasto.safety.session import open_passive_session\nopen_passive_session.__kwdefaults__['library'] = fake",
        "import lasto.safety\nlasto.safety.__path__.insert(0, 'elsewhere')",
        "from lasto.safety import policy\nvars(policy)['NEVER_SERVICES'] = frozenset()",
        "from lasto.safety import policy\npolicy.__dict__['NEVER_SERVICES'] = frozenset()",
        "from lasto.safety import policy\nsetattr(policy, 'NEVER_SERVICES', frozenset())",
        "from lasto.safety import policy\ndelattr(policy, 'NEVER_SERVICES')",
        "from lasto.safety import policy\nobject.__setattr__(policy, 'NEVER_SERVICES', frozenset())",
        "from lasto.safety.gate import Gate\ntype.__setattr__(Gate, 'submit', f)",
        "from lasto.safety import policy\npolicy.__setattr__('NEVER_SERVICES', frozenset())",
        "from lasto.safety import pcan_dll\nmonkeypatch.setattr(pcan_dll, 'POINTER_BYTES', 4)",
        "monkeypatch.setattr('lasto.safety.policy.NEVER_SERVICES', frozenset())",
        "from unittest import mock\nmock.patch('lasto.safety.policy.check_frame', always_fine)",
        "from unittest import mock\nfrom lasto.safety import policy\nmock.patch.object(policy, 'check_frame', always_fine)",
        "from lasto.safety import policy\nfor policy.PADDING_BYTE in range(3):\n    pass",
    ],
)
def test_every_change_to_the_safety_core_is_caught(snippet):
    assert changes_to_the_safety_core("tests.probe", ast.parse(snippet))


@pytest.mark.parametrize(
    "snippet",
    [
        "exchange._rx_id = 0x7E8",  # an instance the test made
        "self._armed = True",
        "sim.dll.write_status = 5",
        "sim.clock.sleep = sleep_and_replug",
        "from lasto.safety.audit import REFUSALS\nREFUSALS.reset()",
        "from lasto.safety.ratelimit import PURPOSE_RATES\nrates = {**PURPOSE_RATES}\nrates['x'] = 1.0",
        "import ctypes\nmonkeypatch.setattr(ctypes, 'WinDLL', missing)",
        "import sys\nmonkeypatch.setattr(sys, 'argv', ['lasto'])",
        "import os\nos.environ['SystemRoot'] = 'D:'",
        "from lasto.safety.session import open_polled_session\nsession = open_polled_session('x')\nsession.request(r)",
    ],
)
def test_changing_what_the_code_made_itself_is_not_flagged(snippet):
    assert changes_to_the_safety_core("tests.probe", ast.parse(snippet)) == []


def test_global_statements_are_caught_only_in_the_safety_core():
    snippet = ast.parse("def rebind():\n    global NEVER_SERVICES\n    NEVER_SERVICES = frozenset()")
    assert changes_to_the_safety_core("lasto.safety.probe", snippet)
    assert changes_to_the_safety_core("lasto.cli", snippet) == []


def test_nothing_anywhere_changes_the_safety_core():
    """Tests may change instances they made, never safety modules, classes, or module-level objects."""
    everything = {**sources(), **sources_of_tests()}
    assert "tests.conftest" in everything and "lasto.safety.gate" in everything
    found = [f"{where}: {change}" for module, tree in everything.items() for change, where in changes_to_the_safety_core(module, tree)]
    assert found == []


def test_no_deliberate_routes_outside_the_exemption_list():
    found = []
    for module, tree in sources().items():
        found += [(route, where) for route, where in deliberate_routes(module, tree) if (route, module) not in EXEMPTIONS]
    assert found == []


def test_every_exemption_is_still_needed():
    used = {(route, module) for module, tree in sources().items() for route, _ in deliberate_routes(module, tree)}
    assert set(EXEMPTIONS) <= used, sorted(set(EXEMPTIONS) - used)
