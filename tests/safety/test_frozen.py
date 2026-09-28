"""Finding #2: the policy can't be changed at runtime.

Every safety module refuses to rebind or delete its names. Every class the
safety core defines refuses changes to its attributes, and so do enum
members. Every module-level table is an immutable type, and instances hold
their state in private slots that only the safety core touches.

The attempts here are made dynamically (importlib, setattr) on purpose: the
static test in test_structure_reach.py bans writing them as code. If an
attempt ever goes through, the test puts the original back before failing,
so nothing leaks into other tests.
"""

from __future__ import annotations

import ast
import dataclasses
import datetime
import enum
import importlib
import pkgutil
import re
import types
import typing
from pathlib import Path

import pytest
from helpers import events
from scan import in_safety, sources

import lasto.safety
from lasto.safety.audit import REFUSALS, Auditor, MemoryAuditSink
from lasto.safety.errors import SafetyViolation

SENTINEL = object()
MISSING = object()

SAFETY_MODULES = [
    importlib.import_module(name)
    for name in sorted(
        ["lasto.safety", *(f"lasto.safety.{info.name}" for info in pkgutil.iter_modules(lasto.safety.__path__))]
    )
]


def safety_classes() -> list[type]:
    return [
        value
        for module in SAFETY_MODULES
        for value in vars(module).values()
        if isinstance(value, type) and value.__module__ == module.__name__
    ]


def went_through(target: object, name: str, *, delete: bool) -> str | None:
    """Try one change that must be refused. Returns how it went wrong, after undoing it; None if refused."""
    own = vars(target) if hasattr(target, "__dict__") else {}
    original = own.get(name, MISSING)
    try:
        if delete:
            delattr(target, name)
        else:
            setattr(target, name, SENTINEL)
    except SafetyViolation:
        return None
    except Exception as exc:  # refused, but not by the safety core
        return f"{type(exc).__name__}: {exc}"
    if original is MISSING:
        delattr(target, name)
    else:
        setattr(target, name, original)
    return "went through"


def attempts(target: object, names: typing.Iterable[str], label: str) -> list[str]:
    found = []
    for name in [*names, "added_later"]:
        for delete in (False, True):
            if delete and name == "added_later":
                continue
            outcome = went_through(target, name, delete=delete)
            if outcome is not None:
                found.append(f"{'del' if delete else 'set'} {label}.{name}: {outcome}")
    return found


def test_every_safety_module_is_frozen():
    """Every .py file in the package, found on disk, imports as a FrozenModule: none skipped its freeze line."""
    frozen_module = importlib.import_module("lasto.safety._frozen").FrozenModule
    on_disk = {
        "lasto.safety" if path.stem == "__init__" else f"lasto.safety.{path.stem}"
        for path in Path(lasto.safety.__file__).parent.glob("*.py")
    }
    assert on_disk == {module.__name__ for module in SAFETY_MODULES}
    assert [module.__name__ for module in SAFETY_MODULES if type(module) is not frozen_module] == []


def test_freeze_is_the_last_statement_of_every_safety_module():
    """Top-level code runs straight into the module's namespace, past its guard, so nothing may follow the freeze."""
    safety = {name: tree for name, tree in sources().items() if in_safety(("module", name))}
    assert len(safety) == len(SAFETY_MODULES)
    assert {name: ast.unparse(tree.body[-1]) for name, tree in safety.items() if ast.unparse(tree.body[-1]) != "freeze(__name__)"} == {}


def test_every_safety_module_refuses_changes_to_its_names():
    assert len(SAFETY_MODULES) >= 20
    found = []
    for module in SAFETY_MODULES:
        found += attempts(module, list(vars(module)), module.__name__)
    assert found == []


def test_every_class_in_the_safety_core_refuses_changes():
    classes = safety_classes()
    assert len(classes) >= 40
    found = []
    for cls in classes:
        found += attempts(cls, list(vars(cls)), f"{cls.__module__}.{cls.__qualname__}")
    assert found == []


def test_enum_members_refuse_changes():
    members = [member for cls in safety_classes() if issubclass(cls, enum.Enum) for member in cls]
    assert len(members) >= 20
    found = []
    for member in members:
        found += attempts(member, ["_name_", "_value_"], repr(member))
    assert found == []


def _immutable(value: object) -> bool:
    if isinstance(value, int | float | complex | str | bytes | type(None) | re.Pattern | enum.Enum | datetime.tzinfo):
        return True
    if isinstance(value, tuple | frozenset):
        return all(_immutable(item) for item in value)
    if isinstance(value, types.MappingProxyType):
        return all(_immutable(key) and _immutable(item) for key, item in value.items())
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return type(value).__dataclass_params__.frozen and all(  # type: ignore[attr-defined]
            _immutable(getattr(value, f.name)) for f in dataclasses.fields(value)
        )
    return False


def _acceptable(name: str, value: object) -> bool:
    if name.startswith("__") and name.endswith("__"):
        return True  # module metadata; the package path is checked on its own
    if _immutable(value) or type(value) is object:  # a plain object() is an identity token
        return True
    if isinstance(value, types.FunctionType | types.BuiltinFunctionType | type | types.ModuleType):
        return True  # module and class changes are covered by the tests above
    if isinstance(value, types.UnionType | types.GenericAlias) or type(value).__module__ == "typing":
        return True  # type annotations
    if name == "annotations":  # from __future__ import annotations
        return True
    return value is REFUSALS  # the process-wide refusal log: stateful by design, private slots only


def test_every_module_level_value_is_immutable():
    found = [
        f"{module.__name__}.{name}: {type(value).__name__}"
        for module in SAFETY_MODULES
        for name, value in vars(module).items()
        if not _acceptable(name, value)
    ]
    assert found == []


def test_the_package_is_frozen_too():
    package = importlib.import_module("lasto.safety")
    assert isinstance(package.__path__, tuple)  # a list here could send later imports elsewhere
    assert went_through(package, "policy", delete=False) is None
    with pytest.raises(SafetyViolation):
        setattr(package, "policy", package.policy)  # even the same module, once it is bound


# Kinds whose instances keep a __dict__ by nature and carry no policy of their own: exceptions (add_note
# needs it), enum members (sealed separately, above), ctypes structures (made fresh for each call),
# protocols (never instantiated), metaclasses, and module types.
def _instance_rules_apply(cls: type) -> bool:
    import ctypes

    return not (
        issubclass(cls, BaseException | enum.Enum | ctypes.Structure | type | types.ModuleType)
        or getattr(cls, "_is_protocol", False)
    )


def test_instances_hold_their_state_in_private_slots():
    found = []
    for cls in filter(_instance_rules_apply, safety_classes()):
        name = f"{cls.__module__}.{cls.__qualname__}"
        if any("__slots__" not in vars(klass) for klass in cls.__mro__ if klass is not object):
            found.append(f"{name} has no __slots__ somewhere in its MRO")
            continue
        public = [slot for klass in cls.__mro__ for slot in vars(klass).get("__slots__", ()) if not slot.startswith("_")]
        frozen = dataclasses.is_dataclass(cls) and cls.__dataclass_params__.frozen  # type: ignore[attr-defined]
        if public and not frozen:
            found.append(f"{name} has public slots {public}")
    assert found == []


def test_a_refused_change_is_audited(clock):
    sink = MemoryAuditSink()
    REFUSALS.attach(Auditor(sink, clock))
    policy = importlib.import_module("lasto.safety.policy")
    assert went_through(policy, "NEVER_SERVICES", delete=False) is None
    [refusal] = events(sink, "rejected")
    assert (refusal["reason"], refusal["transport"]) == ("safety_core_frozen", "core")
    assert "lasto.safety.policy.NEVER_SERVICES" in refusal["detail"]


def test_the_routes_named_in_the_review_are_closed():
    """Reassignments and in-place changes that used to change the policy, each tried and refused."""
    names = {
        "lasto.safety.policy": ["NEVER_SERVICES", "ALLOWED_SERVICES", "OBD_SERVICES", "check_service", "check_frame"],
        "lasto.safety.ecus": ["APPROVED_ECUS", "is_approved", "by_request_id"],
        "lasto.safety.stn_policy": ["EXACT_COMMANDS", "RESET_COMMANDS", "check_command"],
        "lasto.safety.ratelimit": ["HARD_CEILING_PER_SECOND", "PURPOSE_RATES", "CEILING_FRAMES"],
    }
    found = []
    for module_name, targets in names.items():
        found += attempts(importlib.import_module(module_name), targets, module_name)
    assert found == []
    ratelimit = importlib.import_module("lasto.safety.ratelimit")
    ecus = importlib.import_module("lasto.safety.ecus")
    requests = importlib.import_module("lasto.safety.requests")
    for table, key, value in [
        (ratelimit.PURPOSE_RATES, requests.Purpose.LOGGING, 1000.0),
        (vars(ecus)["_BY_REQUEST_ID"], 0x7E1, ecus.ENGINE),
    ]:
        before = dict(table)
        try:
            with pytest.raises(TypeError):
                table[key] = value  # an in-place change to a table
        finally:
            if dict(table) != before:
                table.clear()
                table.update(before)
