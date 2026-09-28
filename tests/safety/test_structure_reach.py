"""Findings #1 (and the static half of #2): nothing reaches around the safety core.

- Code outside the safety core may only use the safety core's public API.
  Names are resolved through every re-export and attribute chain, so
  `from lasto.safety.session import open_active` counts as using
  lasto.safety.pcan_active.open_active.
- No code anywhere in src/ uses a deliberate route around the core's guards
  (ctypes, gc, inspect, importlib, sys.modules, vars/globals/setattr/delattr,
  getattr with a private or computed name, attribute-guard dunders, or,
  outside the safety core, another object's private attributes), except the
  exemptions listed below. Adding an exemption is its own commit, approved by
  the owner.
"""

from __future__ import annotations

import ast

import pytest
from scan import deliberate_routes, in_safety, sources, uses

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
    "lasto.safety.stn_port": {"StnAdapter", "open_serial"},
}  # fmt: skip

# The simulator stands in for PCANBasic.dll, so it speaks the DLL's constants and structures.
SIMULATOR_ALSO_USES = {"lasto.safety.pcan_constants"}

# Every allowed deliberate route, with its reason. Keep this minimal; changes need the owner's approval.
EXEMPTIONS = {
    ("ctypes", "lasto.safety.pcan_constants"): "PCAN-Basic message and timestamp structures",
    ("ctypes", "lasto.safety.pcan_dll"): "the read-only binding of PCANBasic.dll",
    ("ctypes", "lasto.safety.pcan_active"): "the one CAN_Write binding and its message buffer",
    ("ctypes", "lasto.safety.hotkey"): "user32/kernel32 calls for the Ctrl+Alt+K kill-switch hotkey",
    ("ctypes", "lasto.sim.pytest_plugin"): "the test hardware firewall wraps ctypes.CDLL.__init__",
    ("sys.modules", "lasto.sim.pytest_plugin"): "the test hardware firewall replaces pyserial with a stub",
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
    ],
)
def test_every_deliberate_route_is_caught(snippet):
    assert deliberate_routes("lasto.probe", ast.parse(snippet))


def test_ordinary_code_is_not_flagged():
    snippet = "getattr(args, 'live', False)\nhasattr(value, 'value')\nself._private = 1\ncls._table\ntype(x).__name__"
    assert deliberate_routes("lasto.probe", ast.parse(snippet)) == []


def test_no_deliberate_routes_outside_the_exemption_list():
    found = []
    for module, tree in sources().items():
        found += [(route, where) for route, where in deliberate_routes(module, tree) if (route, module) not in EXEMPTIONS]
    assert found == []


def test_every_exemption_is_still_needed():
    used = {(route, module) for module, tree in sources().items() for route, _ in deliberate_routes(module, tree)}
    assert set(EXEMPTIONS) <= used, sorted(set(EXEMPTIONS) - used)
