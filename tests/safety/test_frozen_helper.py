"""The freezing helper itself (lasto.safety._frozen), on scratch modules and classes made here.

Changes go through change(), a plain function, because the static test bans
writing them as code against the safety core.
"""

from __future__ import annotations

import ctypes
import sys
import types
from dataclasses import dataclass, field

import pytest
from helpers import events

from lasto.safety import _frozen
from lasto.safety._frozen import (
    FrozenModule,
    SealedEnum,
    SealedEnumType,
    SealedProtocolType,
    SealedType,
    freeze,
    is_sealed,
    sealed_metaclass,
)
from lasto.safety.audit import REFUSALS, Auditor, MemoryAuditSink
from lasto.safety.errors import SafetyViolation


def change(target: object, name: str, value: object = None, *, delete: bool = False) -> None:
    if delete:
        delattr(target, name)
    else:
        setattr(target, name, value)


def refused(target: object, name: str, value: object = None, *, delete: bool = False) -> bool:
    try:
        change(target, name, value, delete=delete)
    except SafetyViolation as error:
        assert error.reason == "safety_core_frozen"
        return True
    return False


@pytest.fixture
def scratch():
    """Makes modules registered under lasto.safety.* for the test, and removes them afterwards."""
    made: list[str] = []

    def make(name: str, *, package: bool = False) -> types.ModuleType:
        module = types.ModuleType(name)
        if package:
            module.__path__ = []
        sys.modules[name] = module
        made.append(name)
        return module

    yield make
    for name in made:
        sys.modules.pop(name, None)


def test_freeze_seals_the_classes_a_module_defines_and_freezes_the_module(scratch, clock):
    sink = MemoryAuditSink()
    REFUSALS.attach(Auditor(sink, clock))
    module = scratch("lasto.safety.probe_rules")
    plain = SealedType("Plain", (), {"__module__": module.__name__, "LIMIT": 3})
    module.Plain = plain
    module.Borrowed = int  # a class from elsewhere: not this module's to seal
    freeze(module.__name__)
    assert type(module) is FrozenModule and is_sealed(plain) and not is_sealed(int)
    assert refused(plain, "LIMIT", 99) and refused(plain, "LIMIT", delete=True) and refused(plain, "added", 1)
    assert refused(plain, "__class__", type)
    assert refused(module, "LIMIT", 1) and refused(module, "Plain", delete=True) and refused(module, "__class__", types.ModuleType)
    assert plain.LIMIT == 3 and module.Plain is plain
    refusals = events(sink, "rejected")
    assert len(refusals) == 7 and {r["transport"] for r in refusals} == {"core"}
    assert (refusals[0]["reason"], refusals[0]["request"]) == ("safety_core_frozen", "set lasto.safety.probe_rules.Plain.LIMIT")


def test_freeze_refuses_a_class_it_cannot_seal(scratch):
    module = scratch("lasto.safety.probe_unsealable")
    module.Plain = type("Plain", (), {"__module__": module.__name__})  # an ordinary class, no sealed metaclass
    with pytest.raises(SafetyViolation) as caught:
        freeze(module.__name__)
    assert caught.value.reason == "class_not_sealable"
    assert type(module) is types.ModuleType  # left as it was


def test_a_frozen_package_binds_each_submodule_once(scratch):
    package = scratch("lasto.safety.probe_package", package=True)
    freeze(package.__name__)
    assert package.__path__ == ()  # a tuple, so it can't be extended to load other code
    child = scratch("lasto.safety.probe_package.child")
    change(package, "child", child)  # what the import system does when the submodule finishes loading
    assert package.child is child
    assert refused(package, "child", child)  # never again, even with the same module
    assert refused(package, "other", child)  # only under its own name
    assert refused(package, "ghost", None)  # and only a module that is really loaded


def test_a_frozen_module_that_is_not_a_package_binds_no_submodule(scratch):
    """Finding N8: only a package has submodules for the import system to bind."""
    module = scratch("lasto.safety.probe_leaf")
    freeze(module.__name__)
    child = scratch("lasto.safety.probe_leaf.child")  # registered where an import would put it
    assert refused(module, "child", child)
    assert "child" not in vars(module)


def test_classes_and_members_change_freely_until_sealed():
    class Draft(SealedEnum):  # made here and never frozen
        ONLY = 1

    Draft.ONLY.note = "set"
    del Draft.ONLY.note
    Draft.extra = 1
    del Draft.extra

    @dataclass
    class Record(metaclass=SealedType):
        items: list[int] = field(default_factory=list)  # dataclass deletes the class attribute while it builds

    assert not is_sealed(Draft) and not hasattr(Draft.ONLY, "note") and Record().items == []


def test_enum_members_refuse_changes_once_sealed(scratch):
    module = scratch("lasto.safety.probe_enum")

    class Kind(SealedEnum):
        ENGINE = "engine"
        SRS = "srs"

    Kind.__module__ = module.__name__
    module.Kind = Kind
    freeze(module.__name__)
    assert refused(Kind.SRS, "_name_", "ENGINE") and refused(Kind.SRS, "_value_", delete=True)
    assert refused(Kind, "__hash__", lambda self: 0) and refused(Kind, "SRS", 1)
    assert Kind("srs") is Kind.SRS and Kind.SRS in frozenset({Kind.SRS}) and list(Kind) == [Kind.ENGINE, Kind.SRS]


def test_a_sealed_metaclass_works_like_its_base(scratch):
    module = scratch("lasto.safety.probe_struct")
    meta = sealed_metaclass(type(ctypes.Structure))
    message = meta("Message", (ctypes.Structure,), {"__module__": module.__name__, "_fields_": (("ID", ctypes.c_uint32),)})
    module.Message = message
    freeze(module.__name__)
    frame = message()
    frame.ID = 0x7E0
    assert frame.ID == 0x7E0 and is_sealed(meta)
    assert refused(message, "ID", 0)  # the field the write function relies on can't be swapped


def reinit(target: object, *args: object) -> None:
    target.__init__(*args)  # a plain function, like change(): the scanner bans this call in src/


def test_a_sealed_instance_cannot_be_initialized_again(scratch, clock):
    """Finding P1: a frozen dataclass sets its fields with object.__setattr__, so calling __init__ again rewrote it."""
    sink = MemoryAuditSink()
    REFUSALS.attach(Auditor(sink, clock))
    module = scratch("lasto.safety.probe_reinit")

    @dataclass(frozen=True, slots=True)
    class Entry(metaclass=SealedType):
        request_id: int

    class Keeper(metaclass=SealedType):
        __slots__ = ("_value",)

        def __init__(self, value: int) -> None:
            self._value = value

    class Child(Keeper):  # inherits the guarded __init__
        __slots__ = ()

    for cls in (Entry, Keeper, Child):
        cls.__module__ = module.__name__
        setattr(module, cls.__name__, cls)
    freeze(module.__name__)
    entry, keeper, child = Entry(0x7E0), Keeper(1), Child(2)  # a new instance initializes as always
    for target in (entry, keeper, child):
        with pytest.raises(SafetyViolation) as caught:
            reinit(target, 0x0B0)
        assert caught.value.reason == "safety_core_frozen"
    assert (entry.request_id, keeper._value, child._value) == (0x7E0, 1, 2)
    refusals = events(sink, "rejected")
    assert [r["request"] for r in refusals] == [
        f"initialize {module.__name__}.{cls.__qualname__} again" for cls in (Entry, Keeper, Child)
    ]
    assert {r["transport"] for r in refusals} == {"core"}


def test_a_subclass_with_no_slots_of_its_own_is_still_guarded(scratch):
    """A dataclass that adds no fields keeps its state in its parent's slots, and its own __init__ sets them."""
    module = scratch("lasto.safety.probe_inherited_slots")

    @dataclass(frozen=True, slots=True)
    class Entry(metaclass=SealedType):
        request_id: int

    @dataclass(frozen=True, slots=True)
    class Same(Entry):
        pass

    for cls in (Entry, Same):
        cls.__module__ = module.__name__
        setattr(module, cls.__name__, cls)
    freeze(module.__name__)
    same = Same(0x7E0)
    with pytest.raises(SafetyViolation):
        reinit(same, 0x0B0)
    assert same.request_id == 0x7E0


def test_copies_of_a_sealed_instance_still_work(scratch):
    """copy and deepcopy build a new instance and restore its state once, so they aren't refused."""
    import copy

    module = scratch("lasto.safety.probe_copies")

    @dataclass(frozen=True, slots=True)
    class Entry(metaclass=SealedType):
        request_id: int

    Entry.__module__ = module.__name__
    module.Entry = Entry
    freeze(module.__name__)
    entry = Entry(0x7E0)
    assert copy.copy(entry) == entry and copy.deepcopy(entry) == entry and copy.copy(entry) is not entry


def test_only_instances_that_keep_their_state_in_slots_are_guarded(scratch):
    """Exceptions and enum members keep a __dict__ by nature and carry no policy; their __init__ is left alone."""
    module = scratch("lasto.safety.probe_unguarded")

    class Refusal(Exception, metaclass=SealedType):
        def __init__(self, reason: str) -> None:
            super().__init__(reason)
            self.reason = reason

    Refusal.__module__ = module.__name__
    module.Refusal = Refusal
    freeze(module.__name__)
    error = Refusal("first")
    reinit(error, "second")
    assert error.reason == "second"


def test_a_frozen_module_cannot_be_initialized_again(scratch):
    """ModuleType.__init__ would reset a frozen module's __name__, __doc__ and __spec__ straight in its namespace."""
    module = scratch("lasto.safety.probe_module_init")
    module.__doc__ = "original"
    freeze(module.__name__)
    with pytest.raises(SafetyViolation) as caught:
        reinit(module, "elsewhere", "replaced")
    assert caught.value.reason == "safety_core_frozen"
    assert (module.__name__, module.__doc__) == ("lasto.safety.probe_module_init", "original")


def test_the_helper_freezes_itself():
    """So freeze() can't be swapped for a no-op, nor the guards for looser ones, before other modules use them."""
    assert type(_frozen) is FrozenModule
    names = list(vars(_frozen))
    assert {"freeze", "is_sealed", "sealed_metaclass", "SealedType", "FrozenModule", "_guards", "_refuse"} <= set(names)
    for name in names:
        assert refused(_frozen, name, lambda *args, **kwargs: None), name
        assert refused(_frozen, name, delete=True), name
    assert refused(_frozen, "added_later", None)
    assert _frozen.freeze is freeze


def test_the_sealing_machinery_is_sealed_too():
    assert type(SealedType) is SealedType  # its own metaclass, so its guards can't be swapped out
    for target in (SealedType, SealedEnumType, SealedProtocolType, FrozenModule, SealedEnum):
        assert is_sealed(target)
        for name in ("__setattr__", "__delattr__"):
            assert refused(target, name, None)
            assert refused(target, name, delete=True)
