"""Keeps the safety core from being changed at runtime (review finding #2).

Every safety module ends with `freeze(__name__)`, which:

- seals every class the module defines, so none of its attributes can be set
  or deleted (their metaclasses come from here), and an instance that keeps
  its state in slots can't be initialized a second time (finding P1), and
- turns the module into a FrozenModule, so none of its names can be rebound
  or deleted. The package still lets the import system bind each submodule
  on it once, as that submodule finishes loading, and its search path
  becomes a tuple.

Tables are immutable types (frozenset, tuple, MappingProxyType), so nothing
changes in place either, and instances keep their state in private slots.
Every refused change is audited like any other refusal.

The machinery can't be swapped out: SealedType is its own metaclass, and the
other classes here are sealed with it. What pure Python can't block at
runtime (calling object.__setattr__ or type.__setattr__ directly, frames,
gc, ctypes, builtins) is banned in src/ by tests/safety/test_structure_reach.py.
This module is the only one allowed the introspection that freezing needs;
that test lists the exemptions.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from enum import Enum, EnumType
from typing import NoReturn, Protocol


def _refuse(reason: str, what: str) -> NoReturn:
    # Imported here: audit and errors are frozen with this module's help, so this module can't import them first.
    from lasto.safety.audit import refuse
    from lasto.safety.errors import SafetyViolation

    refuse(SafetyViolation(reason, what), transport="core", request=what)


def is_sealed(cls: type) -> bool:
    """Sealed classes carry themselves as `sealed_class`; a subclass inherits a different value until sealed."""
    return getattr(cls, "sealed_class", None) is cls


def _name(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _guards(base: type) -> tuple[Callable[[type, str, object], None], Callable[[type, str], None]]:
    """__setattr__ and __delattr__ for a metaclass derived from `base`: refuse once the class is sealed."""

    def __setattr__(cls: type, name: str, value: object) -> None:
        if is_sealed(cls):
            _refuse("safety_core_frozen", f"set {_name(cls)}.{name}")
        base.__setattr__(cls, name, value)

    def __delattr__(cls: type, name: str) -> None:
        if is_sealed(cls):
            _refuse("safety_core_frozen", f"delete {_name(cls)}.{name}")
        base.__delattr__(cls, name)

    return __setattr__, __delattr__


def _initialized(instance: object) -> bool:
    """Whether any slot of the instance holds a value yet: its __init__ has already run."""
    for member in type(instance)._sealed_slots:  # type: ignore[attr-defined]
        try:
            member.__get__(instance)
        except AttributeError:
            continue
        return True
    return False


def _once(init: Callable[..., object]) -> Callable[..., None]:
    """`init`, refused (audited) on an instance that is initialized already."""

    def __init__(self: object, *args: object, **kwargs: object) -> None:
        if _initialized(self):
            _refuse("safety_core_frozen", f"initialize {_name(type(self))} again")
        init(self, *args, **kwargs)

    return __init__


def _guard_reinit(cls: type) -> None:
    """Make the class's own __init__ refuse an instance that is initialized already (finding P1).

    A frozen dataclass sets its fields with object.__setattr__, so calling its __init__ again on an existing
    instance would rewrite it in place: the approved ECU entry, a session's reader, the kill switch. Only
    classes whose instances keep all their state in slots are guarded; exceptions and enum members keep a
    __dict__ by nature and carry no policy. A subclass __init__ that calls super().__init__() must do so
    before it sets a slot of its own.
    """
    if cls.__dictoffset__ != 0:
        return
    try:
        inherited = cls._sealed_slots  # type: ignore[attr-defined]  (the nearest sealed parent's)
    except AttributeError:
        inherited = ()
    own = tuple(member for member in vars(cls).values() if isinstance(member, types.MemberDescriptorType))
    cls._sealed_slots = inherited + own  # type: ignore[attr-defined]  (where an instance keeps its state)
    init = vars(cls).get("__init__")
    if init is not None:
        cls.__init__ = _once(init)  # type: ignore[misc]  (the class isn't sealed yet)


def _seal(cls: type) -> None:
    _guard_reinit(cls)
    cls.sealed_class = cls  # type: ignore[attr-defined]  (through the class's own guard, one last time)


class _Bootstrap(type):
    """Creates SealedType, which then becomes its own metaclass. Unused after that."""


class SealedType(type, metaclass=_Bootstrap):
    """The metaclass of the safety core's classes. Once sealed, a class's attributes can't be set or deleted."""

    __setattr__, __delattr__ = _guards(type)


# SealedType guards itself, so its own guards can't be replaced either.
SealedType.__class__ = SealedType
del _Bootstrap
_seal(SealedType)


def sealed_metaclass(base: type) -> type:
    """A sealed metaclass that works like `base` (EnumType, ctypes' structure type, and so on)."""
    setattr_guard, delattr_guard = _guards(base)
    name = f"Sealed{base.__name__}"
    namespace = {"__setattr__": setattr_guard, "__delattr__": delattr_guard, "__module__": __name__, "__qualname__": name}
    meta = SealedType(name, (base,), namespace)
    _seal(meta)
    return meta


SealedEnumType = sealed_metaclass(EnumType)
SealedProtocolType = sealed_metaclass(type(Protocol))


class SealedEnum(Enum, metaclass=SealedEnumType):
    """Base for the safety core's enums. Once sealed, members can't change either: a member's name is its hash."""

    def __setattr__(self, name: str, value: object) -> None:
        if is_sealed(type(self)):
            _refuse("safety_core_frozen", f"set {self!r}.{name}")
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if is_sealed(type(self)):
            _refuse("safety_core_frozen", f"delete {self!r}.{name}")
        super().__delattr__(name)


class FrozenModule(types.ModuleType, metaclass=SealedType):
    """A safety module after freeze(): its names can't be rebound or deleted."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        # A module is already initialized when freeze() makes it a FrozenModule. ModuleType.__init__ would
        # reset its __name__, __doc__ and __spec__ straight in its namespace, past __setattr__ (finding P1).
        _refuse("safety_core_frozen", f"initialize {self.__name__} again")

    def __setattr__(self, name: str, value: object) -> None:
        names = vars(self)
        submodule = sys.modules.get(f"{self.__name__}.{name}")
        # Only a package (it has __path__) has submodules; each is bound once, as it finishes loading (N8).
        if "__path__" in names and submodule is not None and value is submodule and name not in names:
            super().__setattr__(name, value)
            return
        _refuse("safety_core_frozen", f"set {self.__name__}.{name}")

    def __delattr__(self, name: str) -> None:
        _refuse("safety_core_frozen", f"delete {self.__name__}.{name}")


def freeze(name: str) -> None:
    """Seal every class the module defines, then freeze the module. The last line of each safety module."""
    module = sys.modules[name]
    for value in list(vars(module).values()):
        if not isinstance(value, type) or value.__module__ != name or is_sealed(value):
            continue
        if not isinstance(type(value), SealedType):
            _refuse("class_not_sealable", f"{_name(value)} needs a metaclass from lasto.safety._frozen")
        _seal(value)
    path = vars(module).get("__path__")
    if path is not None:
        module.__path__ = tuple(path)  # a package's search path; as a list it could be extended to load other code
    module.__class__ = FrozenModule


freeze(__name__)
